import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from network_monitor.app.alerts import Alerts
from network_monitor.app.config import Config
from network_monitor.app.storage import Store


class LocalAudioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory / "test.sqlite", 7, 1000)
        self.store.start(100)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def alerts(self, output="local", token=""):
        return Alerts(Config(alerts_enabled=True, sound_output=output,
                             media_player="media_player.hub" if output != "local" else ""),
                      self.store, token, MagicMock(), self.directory)

    def test_local_does_not_require_cast_entity_and_legacy_defaults_preserved(self):
        Config(alerts_enabled=True, sound_output="local")
        self.assertEqual(Config(media_player="media_player.hub").sound_output, "cast")
        with self.assertRaises(ValueError):
            Config(sound_output="invalid")
        with self.assertRaises(ValueError):
            Config(alerts_enabled=True, sound_output="both")

    async def test_local_audio_uses_supervisor_pulse_without_core_token(self):
        alerts = self.alerts()
        process = MagicMock(returncode=0)
        process.communicate = AsyncMock(return_value=(b"", b""))
        with patch("network_monitor.app.alerts.asyncio.create_subprocess_exec",
                   new=AsyncMock(return_value=process)) as spawn:
            await alerts.play("down")
        args = spawn.call_args.args
        self.assertEqual(args[0], "paplay")
        self.assertIn("--client-name=Linkwatch", args)
        self.assertEqual(args[-1], str(self.directory / "down.wav"))
        self.assertNotIn("shell", spawn.call_args.kwargs)

    async def test_unavailable_audio_and_player_errors_are_explicit(self):
        alerts = self.alerts()
        with patch("network_monitor.app.alerts.asyncio.create_subprocess_exec",
                   new=AsyncMock(side_effect=FileNotFoundError("missing"))):
            with self.assertRaisesRegex(ValueError, "Cannot start local audio"):
                await alerts.play("down")
        process = MagicMock(returncode=1)
        process.communicate = AsyncMock(return_value=(b"", b"Connection refused"))
        with patch("network_monitor.app.alerts.asyncio.create_subprocess_exec",
                   new=AsyncMock(return_value=process)):
            with self.assertRaisesRegex(ValueError, "Connection refused"):
                await alerts.play("recovery")
        alerts.local_ready = False
        with self.assertRaisesRegex(ValueError, "Local sound files unavailable"):
            await alerts.play("down")

    async def test_audio_timeout_kills_only_owned_player(self):
        alerts = self.alerts()
        process = MagicMock(returncode=None)
        process.communicate = AsyncMock(side_effect=TimeoutError())
        process.wait = AsyncMock()
        with patch("network_monitor.app.alerts.asyncio.create_subprocess_exec",
                   new=AsyncMock(return_value=process)):
            with self.assertRaises(ValueError):
                await alerts.play("down")
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_cancellation_cleans_up_player(self):
        process = MagicMock(returncode=None)
        process.communicate = AsyncMock(side_effect=asyncio.CancelledError())
        process.wait = AsyncMock()
        with patch("network_monitor.app.alerts.asyncio.create_subprocess_exec",
                   new=AsyncMock(return_value=process)):
            with self.assertRaises(asyncio.CancelledError):
                await self.alerts().play_local("down")
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_both_outputs_are_independent_and_partial_failure_visible(self):
        alerts = self.alerts("both")
        alerts.play_local = AsyncMock()
        alerts.play_cast = AsyncMock(side_effect=ValueError("WAN unavailable"))
        with self.assertRaisesRegex(ValueError, "cast: WAN unavailable"):
            await alerts.play("down")
        alerts.play_local.assert_awaited_once()
        alerts.play_cast.assert_awaited_once()
        row = self.store.db.execute(
            "SELECT message FROM events WHERE kind='alert_output'").fetchone()
        self.assertIn("local request accepted", row["message"])


if __name__ == "__main__":
    unittest.main()

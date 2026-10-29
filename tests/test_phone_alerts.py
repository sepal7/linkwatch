import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from network_monitor.app.alerts import Alerts
from network_monitor.app.config import Config
from network_monitor.app.storage import Store


class PhoneAlertTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite", 7, 1000)
        self.store.start(100)
        self.config = Config(alerts_enabled=True, sound_output="none",
                             phone_notify_service="notify.mobile_app_test_phone")

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_phone_only_configuration_is_safe_and_backward_compatible(self):
        self.assertEqual(self.config.sound_output, "none")
        for value in ("notify.admin", "media_player.test", "notify.mobile_app_bad/name"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Config(phone_notify_service=value)
        with self.assertRaises(ValueError):
            Config(alerts_enabled=True, sound_output="none")
        self.assertEqual(Config().phone_notify_service, "")

    async def test_supported_core_notification_action_payload(self):
        session = MagicMock()
        response = MagicMock(status=200)
        session.post.return_value.__aenter__.return_value = response
        alerts = Alerts(self.config, self.store, "test-only-token", session)
        for kind, title in (("down", "Connection interrupted"),
                            ("recovery", "Connection restored"), ("test", "Test notification")):
            await alerts.play(kind)
            self.assertEqual(
                session.post.call_args.args[0],
                "http://supervisor/core/api/services/notify/mobile_app_test_phone")
            payload = session.post.call_args.kwargs["json"]
            self.assertIn(title, payload["title"])
            self.assertEqual(payload["data"]["tag"], "linkwatch-connectivity")
            self.assertNotIn("critical", payload["data"])
            self.assertEqual(session.post.call_args.kwargs["headers"]["Authorization"],
                             "Bearer test-only-token")

    async def test_notification_rejection_and_missing_token_are_visible(self):
        alerts = Alerts(self.config, self.store, "", MagicMock())
        with self.assertRaisesRegex(ValueError, "phone: SUPERVISOR_TOKEN"):
            await alerts.play("down")
        response = MagicMock(status=503)
        alerts.token = "test-only-token"
        alerts.session.post.return_value.__aenter__.return_value = response
        with self.assertRaisesRegex(ValueError, "phone:.*503"):
            await alerts.play("down")

    async def test_phone_failure_does_not_prevent_local_sound(self):
        config = Config(alerts_enabled=True, sound_output="local",
                        phone_notify_service="notify.mobile_app_test_phone")
        alerts = Alerts(config, self.store, "", MagicMock())
        alerts.play_local = AsyncMock()
        alerts.notify_phone = AsyncMock(side_effect=ValueError("API rejected"))
        with self.assertRaisesRegex(ValueError, "phone: API rejected"):
            await alerts.play("down")
        alerts.play_local.assert_awaited_once_with("down")
        message = self.store.db.execute(
            "SELECT message FROM events WHERE kind='alert_output'").fetchone()[0]
        self.assertIn("local request accepted", message)

    async def test_test_alert_is_labeled_and_uses_recovery_tone(self):
        config = Config(alerts_enabled=True, sound_output="local",
                        phone_notify_service="notify.mobile_app_test_phone")
        alerts = Alerts(config, self.store, "", MagicMock())
        alerts.play_local = AsyncMock()
        alerts.notify_phone = AsyncMock()
        await alerts.play("test")
        alerts.play_local.assert_awaited_once_with("recovery")
        alerts.notify_phone.assert_awaited_once_with("test")

    async def test_mute_and_disabled_gate_phone_requests(self):
        alerts = Alerts(self.config, self.store, "", MagicMock())
        self.store.set_setting("muted", "true")
        alerts.enqueue("down", 100)
        self.assertTrue(alerts.queue.empty())
        self.store.set_setting("muted", "false")
        alerts.config = Config(sound_output="none",
                               phone_notify_service="notify.mobile_app_test_phone")
        alerts.enqueue("recovery", 100)
        self.assertTrue(alerts.queue.empty())


if __name__ == "__main__":
    unittest.main()

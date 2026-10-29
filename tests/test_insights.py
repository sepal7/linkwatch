import tempfile
import unittest
from pathlib import Path

from network_monitor.app.insights import incident, insights, summary
from network_monitor.app.storage import Store
from tests.test_monitor import probes


class InsightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite", 7, 1000)
        self.store.start(100)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def record(self, ts, state="online"):
        self.store.record(ts, state, probes("fail" if state == "offline" else "ok"), 15)

    def test_summary_separates_downtime_degraded_and_unobserved(self):
        self.record(100)
        self.record(105, "offline")
        self.record(110, "degraded")
        self.record(115)
        self.record(120)
        data = summary(self.store, 90, 130)
        self.assertEqual(data["seconds"], {
            "online": 10, "offline": 5, "degraded": 5, "unknown": 0, "gap": 20})
        self.assertEqual(data["interruptions"], 1)
        self.assertEqual(data["coverage_percent"], 50)
        self.assertEqual(data["longest_interruption_seconds"], 5)
        self.assertEqual(data["mean_ms"], 16.5)
        self.assertEqual(data["p95_ms"], 16.5)
        self.assertEqual(sum(data["seconds"].values()), 40)

    def test_new_single_sample_failure_counts_immediately(self):
        self.record(105, "offline")
        data = summary(self.store, 100, 110)
        self.assertEqual(data["interruptions"], 1)
        self.assertEqual(data["seconds"]["offline"], 0)
        self.assertEqual(data["samples"], 1)
        self.assertIsNone(data["p95_ms"])

    def test_boundary_does_not_count_an_outage_that_already_ended(self):
        self.record(100, "offline")
        self.record(110)
        data = summary(self.store, 110, 120)
        self.assertEqual(data["interruptions"], 0)
        self.assertEqual(data["seconds"]["offline"], 0)

    def test_overlap_prioritizes_gaps_not_false_uptime(self):
        with self.store.db:
            self.store.db.executemany(
                "INSERT INTO intervals(start,end,state,reason) VALUES (?,?,?,'test')",
                [(100, 200, "online"), (150, 170, "offline"), (155, 160, "gap"),
                 (150, 175, "unknown")],
            )
        data = summary(self.store, 100, 200)
        self.assertEqual(sum(data["seconds"].values()), 100)
        self.assertEqual(data["seconds"]["online"], 75)
        self.assertEqual(data["seconds"]["unknown"], 20)
        self.assertEqual(data["seconds"]["gap"], 5)
        self.assertEqual(data["coverage_percent"], 75)

    def test_restart_gap_excluded_from_comparison(self):
        self.record(100)
        self.record(110)
        self.store.start(200)
        self.record(205)
        data = insights(self.store, 150, 250)
        self.assertFalse(data["comparable"])
        self.assertEqual(data["current"]["seconds"]["gap"], 100)

    def test_equal_covered_periods_and_quantile(self):
        for ts in range(100, 301, 5):
            self.record(ts)
        with self.store.db:
            self.store.db.execute("UPDATE samples SET latency_ms=100 WHERE ts=195")
        data = insights(self.store, 200, 300)
        self.assertTrue(data["comparable"])
        self.assertEqual(data["current"]["coverage_percent"], 100)
        self.assertEqual(data["previous"]["max_ms"], 100)
        self.assertEqual(data["previous"]["p95_ms"], 16.5)
        self.assertEqual(data["current"]["samples"], 20)

    def test_daily_breakdown_and_clipped_periods(self):
        data = insights(self.store, 100, 100 + 7 * 86400)
        self.assertEqual(data["period_seconds"], 86400)
        self.assertEqual(len(data["periods"]), 8)
        self.assertEqual(data["periods"][0]["from"], 100)
        self.assertEqual(data["periods"][-1]["to"], 100 + 7 * 86400)
        self.assertTrue(all(p["coverage_percent"] == 0 for p in data["periods"]))
        self.assertIsNone(data["current"]["mean_ms"])

    def test_incident_evidence_includes_transition_recovery_sample(self):
        self.record(100)
        self.record(105, "offline")
        self.record(110, "offline")
        self.record(115)
        row = self.store.db.execute(
            "SELECT id FROM intervals WHERE state='offline'").fetchone()
        self.store.log(120, "alert_requested", "recovery test")
        data = incident(self.store, row["id"])
        self.assertEqual(data["before"]["ts"], 100)
        self.assertEqual(data["after"]["ts"], 115)
        self.assertEqual(data["first"]["ts"], 105)
        self.assertEqual(data["last"]["ts"], 110)
        self.assertEqual(data["probes"][0]["failed"], 2)
        self.assertEqual(data["probes"][0]["passed"], 0)
        self.assertEqual(len(data["nearby_alert_events"]), 1)
        self.assertFalse(data["active"])
        self.assertIsNone(incident(self.store, 99999))

    def test_gap_detail_does_not_assign_adjacent_samples_to_gap(self):
        self.record(100)
        self.store.start(200)
        self.record(205)
        gap = self.store.db.execute(
            "SELECT id FROM intervals WHERE state='gap' ORDER BY id DESC LIMIT 1").fetchone()
        data = incident(self.store, gap["id"])
        self.assertEqual(data["probes"], [])
        self.assertIsNone(data["first"])
        self.assertEqual(data["after"]["ts"], 205)


if __name__ == "__main__":
    unittest.main()

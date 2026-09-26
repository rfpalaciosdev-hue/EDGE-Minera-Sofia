import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from health_report import HealthReporter, influx_bytes, post_with_health


class HealthTests(unittest.TestCase):
    def reporter(self):
        get = MagicMock(side_effect=[
            SimpleNamespace(status_code=200, json=lambda: {"status": "pass"}),
            SimpleNamespace(status_code=200, text='storage_tsm_files_disk_bytes{a="b"} 100\nstorage_wal_size 20'),
        ])
        return HealthReporter("http://influx", lambda: True, get=get)

    def test_size_missing_is_not_zero(self):
        self.assertIsNone(influx_bytes("# no storage metrics"))
        self.assertEqual(influx_bytes("storage_wal_size 0"), 0)
        for value in ["NaN", "-1", "+Inf"]:
            with self.assertRaises(ValueError):
                influx_bytes("storage_wal_size " + value)

    def test_collect_health_and_mqtt_independent_of_sensor_silence(self):
        reporter = self.reporter()
        reporter.collect()
        snapshot = reporter.snapshot()
        self.assertEqual(snapshot["resources"]["influx_data_bytes"], 120)
        self.assertEqual(snapshot["services"], {"mqtt_connection": "connected", "influx_probe": "ok"})
        snapshot["services"]["mqtt_connection"] = "changed"
        self.assertEqual(reporter.snapshot()["services"]["mqtt_connection"], "connected")
        self.assertEqual(reporter.get.call_count, 2)

    def test_failed_probe_is_not_zero_or_healthy(self):
        reporter = HealthReporter("http://influx", lambda: False, get=MagicMock(side_effect=TimeoutError))
        reporter.collect()
        snapshot = reporter.snapshot()
        self.assertEqual(snapshot["services"], {"mqtt_connection": "disconnected", "influx_probe": "error"})
        self.assertIsNone(snapshot["resources"]["influx_data_bytes"])

    def test_metrics_failure_does_not_invent_influx_unavailable(self):
        reporter = self.reporter()
        reporter.get.side_effect = [
            SimpleNamespace(status_code=200, json=lambda: {"status": "pass"}), TimeoutError()
        ]
        reporter.collect()
        self.assertEqual(reporter.snapshot()["services"]["influx_probe"], "ok")
        self.assertIsNone(reporter.snapshot()["resources"]["influx_data_bytes"])

    def test_ack_errors_and_recovery(self):
        reporter = self.reporter()
        reporter.record_send(503)
        reporter.record_send(None)
        self.assertEqual(reporter.forwarding["consecutive_errors"], 2)
        self.assertIsNone(reporter.forwarding["last_http_status"])
        reporter.record_send(200)
        self.assertEqual(reporter.forwarding["consecutive_errors"], 0)
        self.assertIsNotNone(reporter.forwarding["last_ack_at"])

    def test_post_attaches_cache_without_polling_or_mutating_data(self):
        reporter = self.reporter()
        reporter.collect()
        session = MagicMock()
        session.post.return_value.status_code = 200
        body = {"data": []}
        response = post_with_health(reporter, session, "http://cloud", json=body, timeout=10)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("edge_health", body)
        self.assertIn("edge_health", session.post.call_args.kwargs["json"])
        self.assertEqual(reporter.get.call_count, 2)

    def test_sampler_failure_does_not_stop_post(self):
        reporter = MagicMock()
        reporter.snapshot.side_effect = RuntimeError()
        reporter.record_send.side_effect = RuntimeError()
        session = MagicMock()
        session.post.return_value.status_code = 200
        self.assertEqual(post_with_health(reporter, session, "url", json={"data": []}).status_code, 200)
        self.assertNotIn("edge_health", session.post.call_args.kwargs["json"])

    def test_network_failure_is_reraised_and_counted(self):
        reporter = self.reporter()
        session = MagicMock()
        session.post.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            post_with_health(reporter, session, "url", json={"data": []})
        self.assertEqual(reporter.forwarding["consecutive_errors"], 1)

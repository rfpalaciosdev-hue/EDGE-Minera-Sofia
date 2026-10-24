import ast
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from docker_storage_probe import collect, directory_bytes
from health_report import HealthReporter, post_with_health
from resource_report import CONTAINERS, VOLUMES, MAX_FILE_BYTES, read_resources


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "sample.json"
        self.now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
        stamp = self.now.isoformat()
        self.payload = {
            "schema_version": 1,
            "host": {"status": "ok", "collected_at": stamp, "cpu_percent": 20,
                     "uptime_seconds": 300,
                     "memory": {"total_bytes": 1000, "used_bytes": 400, "available_bytes": 600, "used_percent": 40},
                     "disks": [{"drive": "C:", "total_bytes": 10000, "free_bytes": 7000, "used_bytes": 3000, "used_percent": 30}]},
            "docker": {"status": "ok", "collected_at": stamp,
                       "containers": [{"name": name, "state": "running", "health": "healthy", "restart_count": 0,
                                       "cpu_percent": 180, "memory_used_bytes": 100, "memory_limit_bytes": 1000} for name in CONTAINERS],
                       "storage": {"status": "ok", "collected_at": stamp,
                                   "containers": [{"name": name, "writable_bytes": 10, "log_bytes": 20} for name in CONTAINERS],
                                   "volumes": {name: 100 for name in VOLUMES}, "engine": {
                                       "images_bytes_estimate": 123, "build_cache_bytes_estimate": 0,
                                       "volumes_bytes_estimate": 1000, "containers_bytes_estimate": 30}}},
        }

    def read(self):
        self.path.write_text(json.dumps(self.payload))
        return read_resources(self.path, self.now)

    def test_complete_sample_and_total_excludes_database_images_and_cache(self):
        self.payload["docker"]["storage"]["volumes"]["aim_influxdb_data"] = 10**12
        result = self.read()
        self.assertEqual(result["host"]["memory"]["used_percent"], 40)
        self.assertEqual(result["docker"]["containers"][0]["cpu_percent"], 180)
        self.assertEqual(result["docker"]["storage"]["non_db_mutable_bytes"], 390)
        self.assertEqual(result["docker"]["storage"]["status"], "ok")
        self.assertNotIn("aim_influxdb_data", result["docker"]["storage"]["volumes"])

    def test_stale_host_and_docker_keep_storage_independent(self):
        old = (self.now - timedelta(seconds=181)).isoformat()
        self.payload["host"]["collected_at"] = old
        self.payload["docker"]["collected_at"] = old
        result = self.read()
        self.assertEqual(result["host"]["status"], "stale")
        self.assertIsNone(result["host"]["cpu_percent"])
        self.assertEqual(result["docker"]["status"], "stale")
        self.assertIsNone(result["docker"]["containers"][0]["cpu_percent"])
        self.assertEqual(result["docker"]["storage"]["non_db_mutable_bytes"], 390)

    def test_stale_storage_never_becomes_a_fresh_zero_total(self):
        self.payload["docker"]["storage"]["collected_at"] = (self.now - timedelta(seconds=901)).isoformat()
        result = self.read()["docker"]["storage"]
        self.assertEqual(result["status"], "stale")
        self.assertIsNone(result["non_db_mutable_bytes"])
        self.assertTrue(all(value is None for value in result["volumes"].values()))

    def test_partial_storage_preserves_known_values_without_inventing_total(self):
        self.payload["docker"]["storage"]["status"] = "partial"
        self.payload["docker"]["storage"]["containers"][0]["log_bytes"] = None
        result = self.read()["docker"]["storage"]
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["volumes"]["aim_agent_state"], 100)
        self.assertIsNone(result["non_db_mutable_bytes"])

    def test_missing_truncated_oversized_and_wrong_version_files(self):
        for raw in (None, b'{"host":', b' ' * (MAX_FILE_BYTES + 1), b'[]', b'{"schema_version": 2}'):
            if raw is not None:
                self.path.write_bytes(raw)
            result = read_resources(self.path, self.now)
            self.assertEqual(result["host"]["status"], "unavailable")
            self.assertEqual(result["docker"]["status"], "unavailable")
            self.assertIsNone(result["docker"]["storage"]["non_db_mutable_bytes"])

    def test_allowlist_filters_secrets_extra_containers_and_invalid_numbers(self):
        self.payload["secret"] = "PRIVATE"
        self.payload["host"]["environment"] = {"TOKEN": "PRIVATE"}
        self.payload["docker"]["containers"].append({"name": "other-tenant", "secret": "PRIVATE"})
        self.payload["host"]["cpu_percent"] = float("nan")
        self.payload["host"]["memory"]["used_bytes"] = -1
        self.payload["docker"]["storage"]["volumes"]["aim_agent_state"] = True
        result = self.read()
        self.assertIsNone(result["host"]["cpu_percent"])
        self.assertEqual(result["host"]["status"], "partial")
        self.assertIsNone(result["host"]["memory"]["used_bytes"])
        self.assertIsNone(result["docker"]["storage"]["non_db_mutable_bytes"])
        encoded = json.dumps(result, allow_nan=False)
        self.assertNotIn("PRIVATE", encoded)
        self.assertNotIn("other-tenant", encoded)

    def test_invalid_and_future_timestamps_hide_metrics(self):
        for value in (None, "bad", "2026-10-24T12:00:00", (self.now + timedelta(minutes=5)).isoformat()):
            self.payload["host"]["collected_at"] = value
            self.assertIsNone(self.read()["host"]["cpu_percent"])

    def test_structural_garbage_is_safe(self):
        self.payload["host"] = ["bad"]
        self.payload["docker"]["containers"] = [{"name": []}, None, True]
        self.payload["docker"]["storage"]["volumes"] = False
        json.dumps(self.read(), allow_nan=False)

    def test_forwarding_includes_resources_when_influx_and_mqtt_fail(self):
        for name in ("host", "docker"):
            self.payload[name]["collected_at"] = datetime.now(timezone.utc).isoformat()
        self.path.write_text(json.dumps(self.payload))
        reporter = HealthReporter("http://influx", lambda: False, get=MagicMock(side_effect=TimeoutError), resource_path=str(self.path))
        reporter.collect()
        session = MagicMock()
        session.post.return_value.status_code = 200
        post_with_health(reporter, session, "http://cloud", json={"data": []})
        report = session.post.call_args.kwargs["json"]["edge_health"]
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["host"]["cpu_percent"], 20)
        self.assertEqual(report["services"]["influx_probe"], "error")

    def test_resource_reader_failure_cannot_disable_influx_or_forwarding(self):
        reporter = HealthReporter("http://influx", lambda: True, get=MagicMock(side_effect=TimeoutError))
        fallback = read_resources(self.path, self.now)
        with patch("health_report.read_resources", side_effect=[RuntimeError("bad sample"), fallback]):
            reporter.collect()
        self.assertEqual(reporter.snapshot()["host"]["status"], "unavailable")


class StorageProbeTests(unittest.TestCase):
    def test_log_rotation_filter_symlinks_and_hardlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "id-json.log").write_bytes(b"123")
            (root / "id-json.log.1").write_bytes(b"4567")
            (root / "config.v2.json").write_bytes(b"private-config")
            (root / "link-json.log").symlink_to(root / "config.v2.json")
            os.link(root / "id-json.log", root / "id-json.log.2")
            self.assertEqual(directory_bytes(root, "id-json.log*"), 7)
            self.assertEqual(directory_bytes(root), 21)
            self.assertIsNone(directory_bytes(root / "missing"))

    def test_read_failure_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("docker_storage_probe.os.walk", side_effect=PermissionError):
                self.assertIsNone(directory_bytes(tmp))

    def test_probe_restricts_paths(self):
        for path in ("/etc", "/probe/../etc", "relative"):
            with self.assertRaises(ValueError):
                collect([{"key": "a", "path": path}])


class HealthEndpointTests(unittest.TestCase):
    def endpoints(self, snapshot):
        source = (Path(__file__).resolve().parents[1] / "agent" / "main.py").read_text()
        nodes = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name in ("health", "api_health")]
        for node in nodes:
            node.decorator_list = []
        state = {"jsonify": lambda value: value, "health_reporter": SimpleNamespace(snapshot=lambda: snapshot)}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), __file__, "exec"), state)
        return state

    def test_liveness_and_initializing_snapshot(self):
        endpoints = self.endpoints(None)
        self.assertEqual(endpoints["health"](), {"status": "ok"})
        self.assertEqual(endpoints["api_health"](), ({"status": "initializing"}, 503))

    def test_latest_snapshot_is_available_locally(self):
        sample = {"host": {"status": "unavailable"}}
        self.assertEqual(self.endpoints(sample)["api_health"](), sample)


if __name__ == "__main__":
    unittest.main()

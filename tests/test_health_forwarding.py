"""Exercise the actual Sofia forwarding loop without importing the running agent."""
import ast
import queue
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from health_report import HealthReporter, post_with_health


class ForwardingHealthTests(unittest.TestCase):
    def run_iteration(self, *, query_error=False, status=200, reconnect=False, empty=False, count=1, responses=None):
        source = (Path(__file__).resolve().parents[1] / "agent" / "main.py").read_text()
        function = next(node for node in ast.parse(source).body
                        if isinstance(node, ast.FunctionDef) and node.name == "run_forwarder")
        query_api = MagicMock()
        records = []
        for index in range(count):
            record = MagicMock()
            record.values = {"tag_path": f"plant/tag{index}", "is_online": "True"}
            record.get_time.return_value = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
            record.get_value.return_value = 10
            records.append(record)
        query_api.query.return_value = [] if empty else [SimpleNamespace(records=records)]
        if query_error:
            query_api.query.side_effect = RuntimeError("Influx unavailable")
        session = MagicMock()
        session.post.return_value.status_code = status
        if responses is not None:
            session.post.side_effect = responses
        reporter = HealthReporter("http://influx", lambda: True, get=MagicMock(side_effect=TimeoutError))
        reporter.collect()
        reconnect_queue = queue.Queue()
        if reconnect:
            reconnect_queue.put({"tag_path": "plant/tag", "value": 10})
        state = {
            "time": SimpleNamespace(time=lambda: 0, sleep=MagicMock(side_effect=StopIteration)),
            "DEVICE_TOKEN": "test-token", "TENANT_SLUG": "plant", "SITE_ID": "site",
            "CLOUD_API_URL": "http://cloud", "EDGE_INFLUX_BUCKET": "buffer",
            "reconnect_queue": reconnect_queue, "queue": queue,
            "last_mqtt_msg_time": -1000,
            "health_reporter": reporter, "post_with_health": post_with_health,
            "get_last_sync": MagicMock(return_value="-1h"),
            "set_last_sync": MagicMock(), "query_api": query_api,
            "global_settings": {}, "purge_old_influx_data": MagicMock(),
            "datetime": datetime, "timezone": timezone,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), __file__, "exec"), state)
        with patch.dict(sys.modules, {"requests": SimpleNamespace(Session=lambda: session)}), patch("builtins.print"):
            with self.assertRaises(StopIteration):
                state["run_forwarder"]()
        return state, session

    def test_query_failure_sends_health_only_without_advancing_cursor(self):
        state, session = self.run_iteration(query_error=True)
        session.post.assert_called_once()
        body = session.post.call_args.kwargs["json"]
        self.assertEqual(body["data"], [])
        self.assertEqual(body["edge_health"]["services"]["influx_probe"], "error")
        state["set_last_sync"].assert_not_called()

    def test_non_200_keeps_existing_cursor(self):
        state, session = self.run_iteration(status=503)
        self.assertEqual(len(session.post.call_args.kwargs["json"]["data"]), 1)
        state["set_last_sync"].assert_not_called()

    def test_success_advances_existing_timestamp_cursor(self):
        state, session = self.run_iteration()
        state["set_last_sync"].assert_called_once_with("2026-09-26T12:00:00+00:00")
        self.assertIn("edge_health", session.post.call_args.kwargs["json"])

    def test_empty_batch_reports_real_mqtt_without_changing_legacy_status(self):
        state, session = self.run_iteration(empty=True)
        body = session.post.call_args.kwargs["json"]
        self.assertEqual(body["data"], [])
        self.assertFalse(body["status"]["scada_connected"])
        self.assertEqual(body["edge_health"]["services"]["mqtt_connection"], "connected")
        state["set_last_sync"].assert_not_called()

    def test_reconnection_also_attaches_health(self):
        state, session = self.run_iteration(reconnect=True, empty=True)
        self.assertEqual(session.post.call_count, 2)
        for call in session.post.call_args_list:
            self.assertIn("edge_health", call.kwargs["json"])
        state["set_last_sync"].assert_not_called()

    def test_split_batch_advances_cursor_only_after_every_chunk_ack(self):
        with patch('health_report.MAX_RECORDS_PER_REQUEST', 1):
            state, session = self.run_iteration(count=2)
        self.assertEqual(session.post.call_count, 2)
        state['set_last_sync'].assert_called_once_with('2026-09-26T12:00:00+00:00')

    def test_failed_second_chunk_keeps_cursor_including_equal_timestamps(self):
        responses = [SimpleNamespace(status_code=200), SimpleNamespace(status_code=413)]
        with patch('health_report.MAX_RECORDS_PER_REQUEST', 1):
            state, session = self.run_iteration(count=2, responses=responses)
        self.assertEqual(session.post.call_count, 2)
        state['set_last_sync'].assert_not_called()

    def test_timeout_after_partial_ack_keeps_cursor(self):
        responses = [SimpleNamespace(status_code=200), TimeoutError()]
        with patch('health_report.MAX_RECORDS_PER_REQUEST', 1):
            state, session = self.run_iteration(count=2, responses=responses)
        self.assertEqual(session.post.call_count, 2)
        state['set_last_sync'].assert_not_called()

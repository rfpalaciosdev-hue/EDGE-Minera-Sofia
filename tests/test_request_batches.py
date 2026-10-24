import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
from health_report import iter_request_bodies, post_with_health


class RequestBatchTests(unittest.TestCase):
    def test_count_limit_keeps_every_record_in_original_order(self):
        data = [{'value': index} for index in range(1201)]
        body = {'tenant_slug': 'plant', 'data': data, 'status': {'is_online': True}}
        chunks = list(iter_request_bodies(body))
        self.assertEqual([len(chunk['data']) for chunk in chunks], [500, 500, 201])
        self.assertEqual([row for chunk in chunks for row in chunk['data']], data)
        self.assertEqual(len(body['data']), 1201)
        self.assertTrue(all(chunk['status'] == body['status'] for chunk in chunks))

    def test_wire_bytes_include_health_and_escaped_unicode(self):
        data = [{'tag_path': 'temperatura/\u00f1' * 10, 'value': index} for index in range(20)]
        body = {'data': data, 'edge_health': {'sample': 'x' * 200}}
        chunks = list(iter_request_bodies(body, max_bytes=700))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(json.dumps(chunk, allow_nan=False).encode('utf-8')) <= 700 for chunk in chunks))
        self.assertTrue(all(chunk['edge_health'] == body['edge_health'] for chunk in chunks))
        self.assertEqual([row for chunk in chunks for row in chunk['data']], data)

    def test_optional_health_cannot_make_the_envelope_too_large(self):
        body = {'data': [{'value': 1}], 'edge_health': {'sample': 'x' * 1000}}
        chunks = list(iter_request_bodies(body, max_bytes=100))
        self.assertEqual(chunks, [{'data': [{'value': 1}]}])
        self.assertIn('edge_health', body)

    def test_optional_health_does_not_block_a_record_that_fits_by_itself(self):
        body = {'data': [{'value': 'x' * 50}], 'edge_health': {'sample': 'y' * 40}}
        self.assertEqual(list(iter_request_bodies(body, max_bytes=100)), [{'data': body['data']}])

    def test_empty_heartbeat_omits_oversized_optional_health(self):
        body = {'data': [], 'edge_health': {'sample': 'x' * 1000}}
        self.assertEqual(list(iter_request_bodies(body, max_bytes=100)), [{'data': []}])

    def test_single_oversized_record_is_not_dropped_or_truncated(self):
        body = {'data': [{'tag_path': 'x' * 1000}]}
        with self.assertRaisesRegex(ValueError, 'record exceeds'):
            list(iter_request_bodies(body, max_bytes=100))
        self.assertEqual(len(body['data'][0]['tag_path']), 1000)

    def test_empty_heartbeat_remains_one_request(self):
        self.assertEqual(list(iter_request_bodies({'data': []})), [{'data': []}])

    def test_non_200_stops_remaining_chunks(self):
        reporter = MagicMock()
        reporter.snapshot.return_value = None
        session = MagicMock()
        session.post.side_effect = [SimpleNamespace(status_code=200), SimpleNamespace(status_code=503)]
        with patch('health_report.MAX_RECORDS_PER_REQUEST', 1):
            response = post_with_health(reporter, session, 'url', json={'data': [1, 2, 3]})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual([call.args[0] for call in reporter.record_send.call_args_list], [200, 503])

    def test_multipart_network_error_is_propagated_and_accounted(self):
        reporter = MagicMock()
        reporter.snapshot.return_value = None
        session = MagicMock()
        session.post.side_effect = [SimpleNamespace(status_code=200), TimeoutError()]
        with patch('health_report.MAX_RECORDS_PER_REQUEST', 1), self.assertRaises(TimeoutError):
            post_with_health(reporter, session, 'url', json={'data': [1, 2, 3]})
        self.assertEqual([call.args[0] for call in reporter.record_send.call_args_list], [200, None])

    def test_large_multiseries_backfill_fits_http_limit_without_losing_records(self):
        # Reproduce a combined result larger than the server's configured 8 MiB.
        records = [{'tag_path': 'sensor/' + 'x' * 2000, 'value': index} for index in range(5000)]
        self.assertGreater(len(json.dumps({'data': records}).encode()), 8 * 1024 * 1024)
        reporter = MagicMock()
        reporter.snapshot.return_value = {'schema_version': 1, 'resources': {'influx_data_bytes': 0}}
        session = MagicMock()

        def bounded_receiver(url, *, json, **kwargs):
            size = len(__import__('json').dumps(json, allow_nan=False).encode())
            return SimpleNamespace(status_code=200 if size <= 256 * 1024 else 413)

        session.post.side_effect = bounded_receiver
        response = post_with_health(reporter, session, 'url', json={'data': records})
        self.assertEqual(response.status_code, 200)
        self.assertGreater(session.post.call_count, 1)
        forwarded = [row for call in session.post.call_args_list for row in call.kwargs['json']['data']]
        self.assertEqual(forwarded, records)

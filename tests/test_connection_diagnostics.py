import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

source = Path(__file__).resolve().parents[1] / 'scripts' / 'diagnostics' / 'edge_connection.py'
spec = importlib.util.spec_from_file_location('edge_connection_diagnostic', source)
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


class ConnectionDiagnosticTests(unittest.TestCase):
    def environment(self):
        return {'CLOUD_API_URL': 'https://plant.example/api/v1/tenant/edge/sync?secret=PRIVATE',
                'DEVICE_TOKEN': 'PRIVATE', 'TENANT_SLUG': 'plant', 'SITE_ID': 'line1'}

    def test_only_get_requests_without_tokens_or_query_parameters(self):
        get = MagicMock(side_effect=[
            SimpleNamespace(status_code=200, json=lambda: {'collected_at': 'stamp', 'forwarding': {'consecutive_errors': 30, 'token': 'PRIVATE'}}),
            SimpleNamespace(status_code=200, json=lambda: {'pong': True, 'token': 'PRIVATE'}),
        ])
        report = diagnostic.collect(self.environment(), get=get, resolve=MagicMock())
        self.assertEqual(report['local_agent']['consecutive_errors'], 30)
        self.assertTrue(report['cloud_ping']['pong'])
        self.assertEqual(get.call_args_list[1].args[0], 'https://plant.example/api/v1/tenant/edge/ping')
        self.assertNotIn('PRIVATE', json.dumps(report))
        self.assertNotIn('plant.example', json.dumps(report))
        self.assertNotIn('headers', get.call_args.kwargs)
        self.assertFalse(get.call_args.kwargs['allow_redirects'])

    def test_local_failure_does_not_prevent_public_ping(self):
        get = MagicMock(side_effect=[TimeoutError('PRIVATE'), SimpleNamespace(status_code=403)])
        report = diagnostic.collect(self.environment(), get=get, resolve=MagicMock())
        self.assertEqual(report['local_agent']['error'], 'timeout')
        self.assertEqual(report['cloud_ping']['http_status'], 403)
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_network_failure_does_not_print_exception_credentials(self):
        get = MagicMock(side_effect=TimeoutError('PRIVATE'))
        resolve = MagicMock(side_effect=OSError('getaddrinfo failed PRIVATE'))
        report = diagnostic.collect(self.environment(), get=get, resolve=resolve)
        self.assertFalse(report['dns_ok'])
        self.assertEqual(report['cloud_ping']['error'], 'timeout')
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_invalid_targets_are_not_requested(self):
        for target in ('', 'file:///etc/passwd', 'https://user:PRIVATE@plant.example/api/sync', 'https://plant.example/unexpected'):
            get = MagicMock(side_effect=TimeoutError)
            report = diagnostic.collect({'CLOUD_API_URL': target}, get=get, resolve=MagicMock())
            self.assertEqual(get.call_count, 1)  # Local health only.
            self.assertNotIn('PRIVATE', json.dumps(report))

    def test_identity_is_compared_without_exporting_its_value(self):
        get = MagicMock(side_effect=TimeoutError)
        report = diagnostic.collect(self.environment(), get=get, resolve=MagicMock(), expected_site='line1', expected_tenant='other')
        self.assertTrue(report['site_matches_expected'])
        self.assertFalse(report['tenant_matches_expected'])
        self.assertNotIn('line1', json.dumps(report))

    def test_error_classes_are_fixed_without_arbitrary_exception_names(self):
        class PRIVATE(Exception):
            pass
        self.assertEqual(diagnostic.error_kind(PRIVATE('PRIVATE')), 'request_error')


if __name__ == '__main__':
    unittest.main()

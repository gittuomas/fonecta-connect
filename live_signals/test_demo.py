"""Sandbox preflight fails clearly before any owner decision or listing write."""
import os
import unittest
from unittest.mock import patch

from .demo import preflight
from step5_apply.yext import YextError


class SandboxPreflight(unittest.TestCase):
    def setUp(self):
        self.checks = {'mode': 'live', 'checks': [{'entity_id': 'FI-1', 'id': 'live-1', 'date': '2026-11-14'}]}
        env = patch.dict(os.environ, {'YEXT_API_KEY': 'test-not-a-key', 'YEXT_BASE_URL': 'https://sbx-api.yextapis.com',
                                     'YEXT_SANDBOX_MIRROR': 'FI-1:Test sandbox'})
        env.start(); self.addCleanup(env.stop)
        self.client_patch = patch('live_signals.demo.YextClient')
        self.client = self.client_patch.start().return_value; self.addCleanup(self.client_patch.stop)
        self.client.get_entity.return_value = {'hours': {}}

    def test_preflight_reads_mapped_entity_without_writing(self):
        result = preflight(self.checks, True)
        self.assertTrue(result['read_verified'])
        self.client.get_entity.assert_called_once_with('Test sandbox')
        self.client.update_entity.assert_not_called()

    def test_missing_key_unmapped_check_and_production_url_do_not_fall_back(self):
        for env in ({'YEXT_API_KEY': ''}, {'YEXT_SANDBOX_MIRROR': 'OTHER:Test sandbox'},
                    {'YEXT_BASE_URL': 'https://api.yextapis.com'}):
            with self.subTest(env=tuple(env)), patch.dict(os.environ, env), self.assertRaises(ValueError):
                preflight(self.checks, True)
        self.client.get_entity.assert_not_called()

    def test_replay_cannot_reach_sandbox(self):
        self.checks['mode'] = 'recorded_replay'
        with self.assertRaisesRegex(ValueError, 'replay'): preflight(self.checks, True)
        self.client.get_entity.assert_not_called()

    def test_api_failure_is_explicit_and_does_not_print_credentials(self):
        self.client.get_entity.side_effect = YextError(401, 'Denied')
        with self.assertRaisesRegex(ValueError, 'HTTP 401') as err: preflight(self.checks, True)
        self.assertNotIn('test-not-a-key', str(err.exception))

    def test_mock_mode_makes_no_sandbox_requests(self):
        self.assertEqual(preflight(self.checks, False)['mode'], 'local_mock')
        self.client.get_entity.assert_not_called()

"""Provider contract and validation; no external inference calls or credentials."""
import io
import json
import os
import unittest
import urllib.error
from datetime import date
from unittest.mock import Mock, patch

import hours_ai


class HoursAI(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'OPENSHIFT_AI_URL': 'https://inference.example',
                                     'OPENSHIFT_AI_TOKEN': 'test-token', 'OPENSHIFT_AI_MODEL': 'test-model'})
        env.start(); self.addCleanup(env.stop)
        self.items = [{'day': date(2026, 12, 24), 'day_name': 'Christmas Eve'},
                      {'day': date(2026, 12, 25), 'day_name': 'Christmas Day'}]
        self.text = 'On Christmas Eve we open at noon and close at 6pm'
        self.row = {'date': '2026-12-24', 'hours': '12:00-18:00', 'evidence': self.text}

    def call(self, changes=None, issues=None, finish='stop', raw=None):
        parsed = {'changes': [self.row] if changes is None else changes, 'issues': issues or []}
        response = io.BytesIO(json.dumps({'choices': [{'finish_reason': finish,
                               'message': {'content': json.dumps(parsed) if raw is None else raw}}]}).encode())
        opener = Mock()
        opener.open.return_value = response
        with patch.object(hours_ai.urllib.request, 'build_opener', return_value=opener):
            result = hours_ai.read_reply(self.text, self.items)
        self.request = opener.open.call_args.args[0]
        return result

    def test_real_request_contract_and_natural_language_draft(self):
        self.assertEqual(self.call(), ({date(2026, 12, 24): '12:00-18:00'}, []))
        self.assertEqual(self.request.full_url, 'https://inference.example/v1/chat/completions')
        self.assertEqual(self.request.get_header('Authorization'), 'Bearer test-token')
        body = json.loads(self.request.data)
        self.assertEqual(body['model'], 'test-model')
        self.assertEqual(body['response_format']['type'], 'json_schema')
        self.assertIn('Christmas Eve', body['messages'][-1]['content'])

    def test_out_of_scope_duplicate_invalid_and_ungrounded_changes_rejected(self):
        for rows in ([dict(self.row, date='2027-12-24')], [self.row, self.row],
                     [dict(self.row, hours='12:99-18:00')], [dict(self.row, hours='regular')],
                     [dict(self.row, evidence='invented')], [dict(self.row, evidence='')],
                     [dict(self.row, hours=None)], [dict(self.row, extra='bad')]):
            with self.subTest(rows=rows):
                found, problems = self.call(changes=rows)
                self.assertEqual(found, {}); self.assertTrue(problems)

    def test_partial_ambiguous_truncated_and_malformed_replies_rejected(self):
        for args in ({'issues': ['Which day?']}, {'finish': 'length'}, {'raw': 'not json'}, {'raw': '[]'}):
            with self.subTest(args=args):
                found, problems = self.call(**args)
                self.assertEqual(found, {}); self.assertTrue(problems)

    def test_timeout_returns_no_draft(self):
        opener = Mock()
        opener.open.side_effect = TimeoutError()
        with patch.object(hours_ai.urllib.request, 'build_opener', return_value=opener):
            found, problems = hours_ai.read_reply(self.text, self.items)
        self.assertEqual(found, {}); self.assertTrue(problems)

    def test_endpoint_variants_and_unsafe_urls(self):
        for suffix in ('', '/v1', '/v1/chat/completions'):
            with patch.dict(os.environ, {'OPENSHIFT_AI_URL': 'https://inference.example' + suffix}):
                self.assertEqual(hours_ai.endpoint(), 'https://inference.example/v1/chat/completions')
        for url in ('http://inference.example', 'https://user:pass@inference.example', 'https://inference.example?token=x'):
            with patch.dict(os.environ, {'OPENSHIFT_AI_URL': url}), self.assertRaises(ValueError):
                hours_ai.endpoint()

    def test_partial_configuration_does_not_select_pattern_fallback(self):
        with patch.dict(os.environ, {'OPENSHIFT_AI_TOKEN': ''}):
            self.assertTrue(hours_ai.configured())
            found, problems = hours_ai.read_reply(self.text, self.items)
            self.assertEqual(found, {}); self.assertTrue(problems)


if __name__ == '__main__':
    unittest.main()

from __future__ import annotations

import unittest
from unittest.mock import patch

from runner.models import USAGE_SOURCES
from web import queries, api
from web.tests import test_api


def run(index, **overrides):
    row = dict(product='workbuddy', model_mode='default', model_ref='', batch_id='b1',
               started_at=None, benchmark_id=f'r{index}', case_name='hello',
               verdict='Pass', duration_ms=100, engine_ms=50)
    row.update(overrides)
    return row


def usage(index, source, tokens, **overrides):
    row = dict(benchmark_id=f'r{index}', source=source, total_tokens=tokens,
               cost_usd=None, model='model-a')
    row.update(overrides)
    return row


class SummaryTests(unittest.TestCase):
    def summarize(self, runs, samples):
        with patch('web.queries._rows', side_effect=[runs, samples]):
            return queries.mode_summary('suite')

    def test_sources_never_mix_and_missing_is_not_zero(self):
        runs = [run(1), run(2), run(3)]
        samples = [usage(1, 'device-api', 100), usage(1, 'session-jsonl', 800),
                   usage(2, 'session-jsonl', 0), usage(3, 'session-jsonl', None)]
        row = self.summarize(runs, samples)[0]
        sources = {s['source']: s for s in row['usage_sources']}
        self.assertNotIn('total_tokens', row)
        self.assertEqual(100, sources['device-api']['total_tokens'])
        self.assertEqual(800, sources['session-jsonl']['total_tokens'])
        self.assertEqual(2, sources['session-jsonl']['token_rows'])
        self.assertEqual(2, row['usage_rows'])  # 每轮只计一次覆盖
        self.assertIsNone(sources['session-jsonl']['cost_usd'])

    def test_every_source_has_its_own_values_and_coverage(self):
        samples = [usage(i, source, 100 + i) for i, source in enumerate(USAGE_SOURCES)]
        row = self.summarize([run(i) for i in range(len(samples))], samples)[0]
        self.assertEqual(list(USAGE_SOURCES), [u['source'] for u in row['usage_sources']])
        self.assertEqual([100+i for i in range(len(samples))], [u['total_tokens'] for u in row['usage_sources']])
        self.assertTrue(all(u['token_rows'] == 1 for u in row['usage_sources']))

    def test_durations_use_median_range_and_paired_difference(self):
        row = self.summarize([
            run(1, duration_ms=100, engine_ms=50),
            run(2, duration_ms=200, engine_ms=None),
            run(3, duration_ms=9000, engine_ms=9001),
            run(4, duration_ms=None, engine_ms=30),
        ], [])[0]
        self.assertEqual({'n': 3, 'median': 200, 'min': 100, 'max': 9000}, row['wall'])
        self.assertEqual({'n': 2, 'median': 24.5, 'min': -1, 'max': 50}, row['gap'])
        self.assertEqual(3, row['internal']['n'])

    def test_request_modes_stay_separate_without_counting_a_run_twice(self):
        rows = self.summarize([run(1), run(2, model_mode='默认模型')], [])
        self.assertEqual(2, len(rows))
        self.assertEqual(2, sum(row['total'] for row in rows))

    def test_no_usage_stays_empty_and_empty_batch_is_not_one_run(self):
        row = self.summarize([run(1, benchmark_id=None, verdict=None)], [])[0]
        self.assertEqual(0, row['total'])
        self.assertEqual(0, row['wall']['n'])
        self.assertEqual([], row['usage_sources'])

    def test_summary_page_uses_measurement_labels_and_source_coverage(self):
        rows = self.summarize([run(1)], [usage(1, 'workbuddy-cli', 0)])
        with patch('web.api._active_job', return_value=None), \
             patch('web.api.queries.get_suite', return_value={'name': '验收', 'suite_id': 's1'}), \
             patch('web.api.queries.mode_summary', return_value=rows):
            response = api.suite(test_api.WebApiTests._request('/suite/s1'), 's1')
        body = response.body.decode()
        self.assertIn('CLI 自报内部耗时', body)
        self.assertIn('外层 − 内部差值', body)
        self.assertIn('workbuddy-cli', body)
        self.assertIn('1 / 1', body)
        self.assertNotIn('其中模型', body)
        self.assertNotIn('其中冷启动', body)


if __name__ == '__main__':
    unittest.main()

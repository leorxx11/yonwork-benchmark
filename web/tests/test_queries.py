from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from runner.models import USAGE_SOURCES
from web import queries, api
from web.tests import test_api


def run(index, **overrides):
    row = dict(product='workbuddy', model_mode='default', model_ref='', batch_id='b1',
               started_at=None, benchmark_id=f'r{index}', case_name='hello',
               verdict='Pass', duration_ms=100, engine_ms=50,
               model_calls_status='disabled')
    row.update(overrides)
    return row


def usage(index, source, tokens, **overrides):
    row = dict(benchmark_id=f'r{index}', source=source, total_tokens=tokens,
               cost_usd=None, model='model-a')
    row.update(overrides)
    return row


def request(index, **overrides):
    row = dict(batch_id='b1', benchmark_id=f'r{index}', attribution='attributed',
               termination='completed', usage_status='observed',
               input_tokens=10, output_tokens=2)
    row.update(overrides)
    return row


class ModelCallCoverageTests(unittest.TestCase):
    """逐请求账本的覆盖统计。核心是三件事必须分得开。"""

    def summarize(self, runs, requests):
        with patch('web.queries._rows', side_effect=[runs, [], requests]):
            return queries.mode_summary('suite')[0]['model_calls']

    def test_disabled_unavailable_and_observed_are_three_things(self):
        block = self.summarize([
            run(1, model_calls_status='observed'),
            run(2, model_calls_status='unavailable'),
            run(3, model_calls_status='disabled'),
        ], [request(1)])
        self.assertEqual(1, block['runs_observed'])
        self.assertEqual(1, block['runs_unavailable'])
        self.assertEqual(1, block['runs_disabled'])

    def test_tokens_only_count_observed_usage_and_report_coverage(self):
        block = self.summarize([run(1, model_calls_status='observed')], [
            request(1, input_tokens=10, output_tokens=2),
            request(1, usage_status='missing', input_tokens=None, output_tokens=None),
        ])
        self.assertEqual(10, block['input_tokens_observed'])   # 已观测小计，不是整轮
        self.assertEqual(1, block['usage_observed'])
        self.assertEqual(1, block['usage_missing'])

    def test_nothing_observed_gives_none_not_zero(self):
        block = self.summarize([run(1, model_calls_status='observed')], [
            request(1, usage_status='missing', input_tokens=None, output_tokens=None),
        ])
        self.assertIsNone(block['input_tokens_observed'])
        self.assertIsNone(block['output_tokens_observed'])

    def test_attributions_and_failures_are_counted_separately(self):
        block = self.summarize([run(1, model_calls_status='observed')], [
            request(1), request(1, attribution='late'),
            request(1, benchmark_id=None, attribution='unattributed'),
            request(1, benchmark_id=None, attribution='rejected', termination='refused'),
            request(1, termination='upstream-error'),
        ])
        self.assertEqual(2, block['attributed'])
        self.assertEqual(1, block['late'])
        self.assertEqual(1, block['unattributed'])
        self.assertEqual(1, block['rejected'])
        self.assertEqual(2, block['failed'])       # refused + upstream-error
        self.assertIsNone(block['upstream_attempts'])   # 网关内部重试看不见


class RequestTimelineTests(unittest.TestCase):
    """`/run/<id>` 的逐请求时间线。"""

    def fetch(self, owned, orphans=()):
        with patch('web.queries._rows', side_effect=[owned, list(orphans)]):
            return queries.get_model_requests('r1')

    @staticmethod
    def _span(start, end, **overrides):
        row = dict(sequence=1, duration_ms=(end - start) * 1000,
                   received_at=datetime(2026, 9, 22, 10, 0, start),
                   ended_at=datetime(2026, 9, 22, 10, 0, end))
        row.update(overrides)
        return row

    def test_serial_requests_can_be_summed(self):
        timing = self.fetch([self._span(0, 1), self._span(2, 3)])['timing']
        self.assertEqual(2000, timing['sum_ms'])
        self.assertFalse(timing['overlapping'])
        self.assertFalse(timing['partial'])

    def test_overlapping_requests_are_flagged(self):
        """并发请求相加会把同一段墙钟算两遍，必须说出来。"""
        timing = self.fetch([self._span(0, 5), self._span(2, 6)])['timing']
        self.assertTrue(timing['overlapping'])

    def test_missing_duration_makes_the_sum_partial(self):
        timing = self.fetch([
            self._span(0, 1),
            self._span(2, 3, duration_ms=None),
        ])['timing']
        self.assertEqual(1000, timing['sum_ms'])
        self.assertTrue(timing['partial'])
        self.assertEqual(1, timing['measured'])
        self.assertEqual(2, timing['total'])

    def test_unattributed_requests_stay_out_of_the_run(self):
        """未归属请求属于批次不属于某一轮，不能并进这一轮的计数。"""
        found = self.fetch([self._span(0, 1)], [self._span(3, 4)])
        self.assertEqual(1, len(found['requests']))
        self.assertEqual(1, len(found['orphans']))
        self.assertEqual(1000, found['timing']['sum_ms'])


class SummaryTests(unittest.TestCase):
    def summarize(self, runs, samples, requests=()):
        # 三次 _rows：轮次、用量、逐请求账本。加查询时这里要跟着加，
        # 否则 side_effect 会用尽并报 StopIteration。
        with patch('web.queries._rows', side_effect=[runs, samples, list(requests)]):
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


class RunPageModelCallTests(unittest.TestCase):
    """`/run/<id>` 对三种采集状态的呈现。差别就是这一节要守住的东西。"""

    def render(self, status, model_calls):
        found = dict(benchmark_id='r1', batch_id='b1', verdict='Pass', duration_ms=3000,
                     first_delta_ms=None, product='yonwork', model_mode='newapi',
                     model_ref='', suite_id=None, suite_name=None, run_id='r1',
                     session_key='k', requested_model='m', terminated_by=None,
                     stop_reason=None, started_at=None, ended_at=None, note='',
                     prompt='p', answer_preview='a', tool_call_count=None,
                     transcript_path=None, model_calls_status=status)
        with patch('web.api._active_job', return_value=None), \
             patch('web.api.queries.get_run', return_value=found), \
             patch('web.api.queries.get_checks', return_value=[]), \
             patch('web.api.queries.get_usage', return_value=[]), \
             patch('web.api.queries.get_model_requests', return_value=model_calls):
            return api.run_detail(test_api.WebApiTests._request('/run/r1'), 'r1').body.decode()

    @staticmethod
    def _empty():
        return {'requests': [], 'orphans': [],
                'timing': {'sum_ms': None, 'measured': 0, 'total': 0,
                           'overlapping': False, 'partial': False}}

    def test_disabled_says_not_collected_not_zero(self):
        body = self.render('disabled', self._empty())
        self.assertIn('没有开逐请求采集', body)
        self.assertIn('不等于这一轮没有调用模型', body)

    def test_unavailable_points_at_the_base_url(self):
        """开着却零请求 = baseUrl 没指过来，不是产品没调模型。"""
        body = self.render('unavailable', self._empty())
        self.assertIn('一个请求都没经过入口', body)
        self.assertIn('baseUrl', body)

    def test_observed_shows_the_timeline_and_refuses_to_subtract(self):
        body = self.render('observed', {
            'requests': [dict(sequence=1, attribution='attributed',
                              attribution_source='x-yonwork-run-id',
                              requested_model='m', response_model='m',
                              first_output_ms=1100, duration_ms=1800, http_status=200,
                              termination='completed', error_kind='',
                              usage_status='observed', input_tokens=16098,
                              output_tokens=126, upstream_request_id='req-1')],
            'orphans': [],
            'timing': {'sum_ms': 1800, 'measured': 1, 'total': 1,
                       'overlapping': False, 'partial': False},
        })
        self.assertIn('req-1', body)
        self.assertIn('16,098', body)
        self.assertIn('不能相减当成任何一方的开销', body)
        self.assertIn('未知', body)          # 上游尝试

    def test_overlapping_requests_are_called_out(self):
        body = self.render('observed', {
            'requests': [dict(sequence=1, attribution='attributed', attribution_source='h',
                              requested_model='m', response_model='m', first_output_ms=1,
                              duration_ms=5000, http_status=200, termination='completed',
                              error_kind='', usage_status='missing', input_tokens=None,
                              output_tokens=None, upstream_request_id=None)],
            'orphans': [],
            'timing': {'sum_ms': 5000, 'measured': 1, 'total': 1,
                       'overlapping': True, 'partial': False},
        })
        self.assertIn('有重叠', body)
        self.assertIn('未采集', body)        # 缺 usage 不显示 0

    def test_orphans_are_listed_apart_from_the_run(self):
        body = self.render('observed', {
            'requests': [],
            'orphans': [dict(sequence=7, requested_model='m', http_status=200,
                             termination='completed', duration_ms=900)],
            'timing': {'sum_ms': None, 'measured': 0, 'total': 0,
                       'overlapping': False, 'partial': False},
        })
        self.assertIn('同批次的未归属请求', body)
        self.assertIn('不知道属于哪一轮', body)


if __name__ == '__main__':
    unittest.main()

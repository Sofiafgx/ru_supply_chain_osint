import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from comtrade_mirror_async import (API_URL, BudgetExceeded, CollectionError, Collector,
                                  Deferred, State, add_intervals, months, normalize,
                                  query_params, retry_after, heading_intervals)


def source_row(**changes):
    row = dict(period='202401', freqCode='M', reporterCode=156, reporterDesc='China',
               partnerCode=643, partner2Code=0, cmdCode='854231', flowCode='X',
               customsCode='C00', motCode=0, primaryValue=150000, fobvalue=160000,
               qty=10, qtyUnitCode=5, qtyUnitAbbr='u', netWgt=0,
               classificationCode='H6')
    row.update(changes)
    return row


class DataTests(unittest.TestCase):
    def test_month_range(self):
        self.assertEqual(len(months('2024-01', '2026-12')), 36)
        with self.assertRaises(ValueError):
            months('2024-1', '2026-12')

    def test_missing_and_zero_preserved_and_units_retained(self):
        row = normalize([source_row(qty=None)], query_params('2024-01', 156, '854231'))[0]
        self.assertEqual(row['trade_value_usd'], 150000)
        self.assertEqual(row['net_weight_kg'], 0)
        self.assertIsNone(row['quantity'])
        self.assertEqual(row['quantity_unit'], 'u')
        self.assertEqual(row['period'], '2024-01')

    def test_no_wrong_country_or_double_counting(self):
        params = query_params('2024-01', 156, '854231')
        with self.assertRaises(CollectionError):
            normalize([source_row(partnerCode=0)], params)
        self.assertEqual(len(normalize([source_row(), source_row()], params)), 1)
        with self.assertRaises(CollectionError):
            normalize([source_row(), source_row(primaryValue=200000)], params)

    def test_intervals_strict_threshold_separate_flows_leap_year(self):
        rows = [dict(period=p, reporter_code=156, hs_code='854231', flow_code=f,
                     trade_value_usd=v) for p, f, v in [
                         ('2024-01', 'X', 150000), ('2024-02', 'X', 100000),
                         ('2024-03', 'X', 200000), ('2024-02', 'RX', 200000)]]
        add_intervals(rows, 100000)
        self.assertEqual(rows[2]['delta_t_months'], 2)
        self.assertEqual(rows[2]['delta_t_days'], 60)
        self.assertFalse(rows[1]['large_monthly_total'])
        self.assertIsNone(rows[3]['delta_t_months'])

    def test_retry_after_formats(self):
        self.assertEqual(retry_after('12'), 12)
        self.assertIsNone(retry_after('nan'))
        with patch('comtrade_mirror_async.time.time', return_value=0):
            self.assertEqual(retry_after('Thu, 01 Jan 1970 00:00:30 GMT'), 30)

    def test_heading_uses_exports_without_double_counting_reexports(self):
        periods=['2024-01','2024-02']
        coverage=[dict(period=p,reporter_code=156,hs_code='854231',status='ok') for p in periods]
        rows=[dict(period=p,reporter_code=156,hs_code='854231',flow_code=f,trade_value_usd=v)
              for p in periods for f,v in [('X',150000),('RX',100000)]]
        result=heading_intervals(rows,coverage,periods,[156],['854231'])['8542']
        self.assertEqual(result['delta_t_interval_days'],31)
        self.assertEqual(result['monthly'][0]['value_usd'],150000)
        incomplete=heading_intervals(rows,coverage[:1],periods,[156],['854231'])['8542']
        self.assertIsNone(incomplete['delta_t_interval_days'])
        self.assertIsNone(incomplete['monthly'][1]['value_usd'])
        absent=heading_intervals([], [dict(period=p,reporter_code=156,hs_code='854231',status='no_data') for p in periods],periods,[156],['854231'])['8542']
        self.assertIsNone(absent['monthly'][0]['value_usd'])
        self.assertIsNone(absent['delta_t_interval_days'])


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.temp.name) / 'cache.sqlite3')

    async def asyncTearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def collector(self, client, **kwargs):
        collector = Collector(client, self.state, **kwargs)
        collector.spacing = 0  # Avoid real-time waits in mocked HTTP tests.
        return collector

    async def test_retry_429_cache_and_budget_count_retries(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.url.path, '/public/v1/preview/C/M/HS')
            if len(calls) == 1:
                return httpx.Response(429, headers={'Retry-After': '0'})
            return httpx.Response(200, json={'data': [source_row()], 'count': 1, 'error': ''})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            collector = self.collector(client)
            params = query_params('2024-01', 156, '854231')
            self.assertEqual(len(await collector.fetch(params)), 1)
            self.assertEqual(len(await collector.fetch(params)), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.state.db.execute('SELECT calls FROM usage').fetchone()[0], 2)

    async def test_429_without_header_waits_60_seconds_and_keeps_query(self):
        calls=[]
        def handler(request):
            calls.append(str(request.url))
            if len(calls)==1:
                return httpx.Response(429)
            return httpx.Response(200,json={'data':[source_row()], 'count':1})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            collector=self.collector(client)
            with patch.object(self.state,'defer') as pause:
                await collector.fetch(query_params('2024-01',156,'854231'))
            self.assertEqual(pause.call_args.args[0],60)
            self.assertEqual(calls[0],calls[1])

    async def test_cap_splits_flows_without_offset(self):
        flows = []
        def handler(request):
            flow = request.url.params['flowCode']
            self.assertNotIn('offset', request.url.params)
            flows.append(flow)
            rows = [source_row()] * 500 if flow == 'X,RX' else [source_row(flowCode=flow)]
            return httpx.Response(200, json={'data': rows, 'count': len(rows)})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            rows = await self.collector(client).fetch(query_params('2024-01', 156, '854231'))
        self.assertEqual(flows, ['X,RX', 'X', 'RX'])
        self.assertEqual(len(rows), 2)

    async def test_unsplittable_cap_and_count_mismatch_not_cached(self):
        for payload in ({'data': [source_row()] * 500, 'count': 500},
                        {'data': [source_row()], 'count': 501}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=payload))) as client:
                params = query_params('2024-01', 156, '854231', 'X')
                with self.assertRaises(CollectionError):
                    await self.collector(client).fetch(params)
                self.assertIsNone(self.state.get(params))

    async def test_empty_data_cached_but_api_error_not_cached(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={'data': [], 'error': ''}))) as client:
            params = query_params('2024-01', 156, '854231')
            self.assertEqual(await self.collector(client).fetch(params), [])
            self.assertEqual(self.state.get(params), [])
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={'data': [], 'error': 'Invalid query'}))) as client:
            with self.assertRaises(CollectionError):
                await self.collector(client, refresh=True).fetch(params)

    async def test_budget_persists_across_restart(self):
        self.state.reserve(1)
        other = State(Path(self.temp.name) / 'cache.sqlite3')
        try:
            with self.assertRaises(BudgetExceeded):
                other.reserve(1)
        finally:
            other.db.close()

    async def test_long_retry_after_halts_all_workers_and_persists(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, headers={'Retry-After': '3600'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            collector = self.collector(client)
            with self.assertRaises(Deferred):
                await collector.fetch(query_params('2024-01', 156, '854231'))
            with self.assertRaises(Deferred):
                await collector.fetch(query_params('2024-02', 156, '854231'))
        self.assertEqual(len(calls), 1)
        self.assertGreater(self.state.cooldown(), 0)


if __name__ == '__main__':
    unittest.main()

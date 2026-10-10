import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from arshin_parser import (ProxySession, ProtocolError, collect, load_proxies,
                           owner_identity, parse_card)


def card(owner='Завод, ИНН 6444009038', **changes):
    mi = {'mitypeTitle': 'Машина координатно-измерительная',
          'mitypeType': 'Zeiss', 'modification': 'Prismo', 'manufactureNum': '0012'}
    mi.update(changes)
    return {'result': {'miInfo': {'singleMI': mi}, 'vriInfo': {
        'miOwner': owner, 'vrfDate': '03.09.2025', 'applicable': {}}}}


class ArshinTests(unittest.TestCase):
    def setUp(self):
        self.targets = {'6444009038': ['АО «Завод»']}
        self.doc = {'vri_id': '1-123', 'applicability': True}

    def test_complete_record_and_date(self):
        row = parse_card(self.doc, card(), self.targets)
        self.assertEqual(row['verification_date'], '2025-09-03')
        self.assertEqual(row['equipment_serial_number'], '0012')
        self.assertEqual(row['verification_result'], 'придатний')
        self.assertEqual(len(row), 8)

    def test_missing_card_fields_do_not_use_index_or_standard(self):
        payload = card(modification='нет данных', manufactureNum=None)
        payload['result']['means'] = {'mis': [{'manufactureNum': 'WRONG'}]}
        doc = dict(self.doc, **{'mi.modification': 'WRONG', 'mi.number': 'WRONG'})
        row = parse_card(doc, payload, self.targets)
        self.assertIsNone(row['equipment_modification'])
        self.assertIsNone(row['equipment_serial_number'])

    def test_verifier_does_not_match_owner(self):
        payload = card(owner='Інша компанія')
        payload['result']['vriInfo']['organization'] = 'Завод ИНН 6444009038'
        self.assertIsNone(parse_card(self.doc, payload, self.targets))

    def test_alias_and_conflicting_inn(self):
        self.assertEqual(owner_identity({'miOwner': 'АО "Завод"'}, self.targets)[1], '6444009038')
        self.assertIsNone(owner_identity({'miOwner': 'АО "Завод"', 'miOwnerInn': '1234567890'}, self.targets))
        self.assertIsNone(owner_identity({'miOwner': 'АО "Завод" филиал'}, self.targets))

    def test_bare_alias_legal_prefix_and_ambiguous_history(self):
        targets = {'2128700223': ['ВНИИР-ПРОГРЕСС']}
        self.assertEqual(owner_identity({'miOwner': 'ЗАО «ВНИИР-ПРОГРЕСС»'}, targets)[1], '2128700223')
        targets['2130094170'] = ['ВНИИР ПРОГРЕСС']
        self.assertIsNone(owner_identity({'miOwner': 'АО «ВНИИР-ПРОГРЕСС»'}, targets))
        self.assertEqual(owner_identity({'miOwner': 'АО «ВНИИР-ПРОГРЕСС»', 'miOwnerInn': '2130094170'}, targets)[1], '2130094170')

    def test_inapplicable_and_unknown_status(self):
        payload = card()
        payload['result']['vriInfo'].pop('applicable')
        payload['result']['vriInfo']['inapplicable'] = {'reason': 'ошибка'}
        self.assertEqual(parse_card(self.doc, payload, self.targets)['verification_result'], 'непридатний')
        payload['result']['vriInfo'].pop('inapplicable')
        with self.assertRaises(ProtocolError):
            parse_card({'vri_id': '1-123'}, payload, self.targets)

    def test_proxy_formats_and_port_range(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'proxies.txt'
            path.write_text('# comment\nhost:10000\nhost:10999:user:password\nhttp://host:11000\n')
            self.assertEqual(load_proxies(path), ['http://host:10000', 'http://user:password@host:10999'])

    def test_sessions_rotate_reuse_and_retry(self):
        factories, requests = [], []
        def factory(**kwargs):
            factories.append(kwargs)
            def handle(request):
                requests.append(kwargs['proxy'])
                if len(requests) == 1:
                    raise httpx.ConnectError('failure', request=request)
                return httpx.Response(200, json={'result': {}})
            return httpx.Client(transport=httpx.MockTransport(handle))
        session = ProxySession(['http://host:10000', 'http://host:10001'], delay=0, client_factory=factory)
        try:
            with patch('arshin_parser.time.sleep'):
                session.get_json('https://example.org')
                session.get_json('https://example.org')
            self.assertEqual(requests, ['http://host:10000', 'http://host:10001', 'http://host:10001'])
            self.assertEqual(len(factories), 2)
            self.assertTrue(all(k['verify'] is False for k in factories))
        finally:
            session.close()

    def test_pagination_and_atomic_failure(self):
        class Session:
            def get_json(_, url, params=None):
                if params is not None:
                    start = params['start']
                    return {'response': {'numFound': 2, 'docs': [{'vri_id': f'1-{start}'}]}}
                return card()
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'result.jsonl'
            stats = collect(Session(), self.targets, output, rows=1)
            self.assertTrue(stats['complete'])
            self.assertEqual(stats['matched'], 2)
            rows = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual([r['vri_id'] for r in rows], ['1-0', '1-1'])
            class Broken:
                def get_json(_, url, params=None):
                    raise RuntimeError('failure')
            before = output.read_bytes()
            with self.assertRaises(RuntimeError):
                collect(Broken(), self.targets, output)
            self.assertEqual(output.read_bytes(), before)

    def test_http499_rotates_proxy_before_aborting(self):
        used = []
        def factory(**kwargs):
            def handle(request):
                used.append(kwargs['proxy'])
                return httpx.Response(499) if len(used) == 1 else httpx.Response(200, json={'result': {}})
            return httpx.Client(transport=httpx.MockTransport(handle))
        session = ProxySession(['http://host:10000', 'http://host:10001'], delay=0, retries=2, client_factory=factory)
        try:
            with patch('arshin_parser.time.sleep'):
                self.assertEqual(session.get_json('https://example.org'), {'result': {}})
            self.assertEqual(used, ['http://host:10000','http://host:10001'])
        finally:
            session.close()

    def test_499_timeout_backoff_and_read_timeout(self):
        used, options = [], []
        def factory(**kwargs):
            options.append(kwargs)
            def handle(request):
                used.append(kwargs['proxy'])
                if len(used) == 1:
                    return httpx.Response(499)
                if len(used) == 2:
                    raise httpx.ReadTimeout('timeout', request=request)
                return httpx.Response(200, json={'ok': True})
            return httpx.Client(transport=httpx.MockTransport(handle))
        session = ProxySession([f'http://host:{port}' for port in range(10000,10003)], delay=0, client_factory=factory)
        try:
            with patch('arshin_parser.time.sleep') as sleep:
                self.assertEqual(session.get_json('https://example.org'), {'ok': True})
            self.assertEqual(len(set(used)), 3)
            self.assertEqual([c.args[0] for c in sleep.call_args_list if c.args[0] > 0], [3, 6])
            self.assertTrue(all(o['timeout'].read == 50 and o['timeout'].connect == 15 for o in options))
        finally:
            session.close()

    def test_retry_exhaustion_and_fatal_status(self):
        for status in (499, 404):
            used = []
            def factory(**kwargs):
                def handle(request):
                    used.append(kwargs['proxy'])
                    return httpx.Response(status)
                return httpx.Client(transport=httpx.MockTransport(handle))
            session = ProxySession(['http://host:10000','http://host:10001'], retries=2, delay=0, client_factory=factory)
            try:
                with patch('arshin_parser.time.sleep') as sleep, self.assertRaises(RuntimeError):
                    session.get_json('https://example.org')
                self.assertEqual(len(used), 2 if status == 499 else 1)
                pauses = [c.args[0] for c in sleep.call_args_list if c.args[0] > 0]
                self.assertEqual(pauses, [3] if status == 499 else [])
            finally:
                session.close()

    def test_append_failure_report_and_restart_deduplication(self):
        class Session:
            def __init__(self, fail=False):
                self.fail = fail
            def get_json(self, url, params=None):
                if params is not None:
                    return {'response': {'numFound': 2, 'docs': [{'vri_id':'1-0'}, {'vri_id':'1-1'}]}}
                if self.fail and url.endswith('1-1'):
                    raise RuntimeError('Запит не виконано')
                return card()
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)/'result.jsonl'
            old = parse_card({'vri_id':'1-old'}, card(), self.targets)
            output.write_text(json.dumps(old)+'\n')
            report = Path(str(output)+'.report.json')
            report.write_text('{"cards":999,"total_candidates":99999}')
            with self.assertRaises(RuntimeError):
                collect(Session(fail=True), self.targets, output)
            state = json.loads(report.read_text())
            self.assertEqual((state['status'], state['cards'], state['matched']), ('failed',1,1))
            self.assertEqual(state['total_candidates'], 2)
            partial = Path(str(output)+'.partial')
            self.assertFalse(partial.exists())
            self.assertEqual(len(output.read_text().splitlines()), 2)
            stats = collect(Session(), self.targets, output)
            self.assertEqual((stats['previously_saved'],stats['cards'],stats['matched']), (2,1,1))
            self.assertEqual(len(output.read_text().splitlines()), 3)
            self.assertFalse(partial.exists())
            self.assertEqual(json.loads(report.read_text())['status'],'completed')


if __name__ == '__main__':
    unittest.main()

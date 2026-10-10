"""SQLite evidence ledger and auditable import of collector snapshots.

A manifest defines inputs, query outcomes and the approved pilot scope. Imports
never invent historical collection timestamps, missing stock or consumption.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlencode, urlsplit

from analytics.resilience import tts, radar_candidate, nonnegative
from eis_ftp_contracts import valid_inn
from data_sanitization import sanitize_record

CATEGORIES = ('found', 'no_result', 'technical_access_unavailable')
KINDS = ('contract', 'court', 'equipment', 'trade', 'inventory', 'consumption', 'radar', 'scenario')

SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS observations (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, input_sha256 TEXT NOT NULL,
 collected_at TEXT, source_url TEXT, ingested_at TEXT NOT NULL,
 input_file TEXT NOT NULL, line_number INTEGER NOT NULL, payload TEXT NOT NULL,
 gaps TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_observations (
 run_id TEXT NOT NULL, observation_id TEXT NOT NULL REFERENCES observations,
 PRIMARY KEY(run_id,observation_id));
CREATE TABLE IF NOT EXISTS entities (inn TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS contracts (
 customer_inn TEXT NOT NULL REFERENCES entities, supplier_inn TEXT NOT NULL REFERENCES entities,
 contract_id TEXT NOT NULL, PRIMARY KEY(customer_inn,supplier_inn,contract_id));
CREATE TABLE IF NOT EXISTS contract_evidence (
 customer_inn TEXT NOT NULL, supplier_inn TEXT NOT NULL, contract_id TEXT NOT NULL,
 observation_id TEXT NOT NULL REFERENCES observations,
 PRIMARY KEY(customer_inn,supplier_inn,contract_id,observation_id),
 FOREIGN KEY(customer_inn,supplier_inn,contract_id) REFERENCES contracts);
CREATE TABLE IF NOT EXISTS court_evidence (
 plaintiff_inn TEXT NOT NULL REFERENCES entities, respondent_inn TEXT NOT NULL REFERENCES entities,
 case_number TEXT NOT NULL, observation_id TEXT NOT NULL REFERENCES observations,
 PRIMARY KEY(plaintiff_inn,respondent_inn,case_number,observation_id));
CREATE TABLE IF NOT EXISTS query_runs (
 run_id TEXT NOT NULL, query_id TEXT NOT NULL, source TEXT NOT NULL,
 category TEXT NOT NULL CHECK(category IN ('found','no_result','technical_access_unavailable')),
 details TEXT NOT NULL, PRIMARY KEY(run_id,query_id));
CREATE TABLE IF NOT EXISTS inventory (
 observation_id TEXT PRIMARY KEY REFERENCES observations, inn TEXT,
 S_safe REAL, D_daily REAL, stock_unit TEXT, demand_unit TEXT, verified INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS consumption (
 observation_id TEXT PRIMARY KEY REFERENCES observations, supplier_inn TEXT,
 product_id TEXT, r_kj REAL, unit TEXT, verified INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS monthly_trade (
 observation_id TEXT PRIMARY KEY REFERENCES observations, period TEXT, reporter_code INTEGER,
 partner_code INTEGER, hs_code TEXT, flow_code TEXT, trade_value_usd REAL,
 net_weight_kg REAL, quantity REAL, quantity_unit TEXT);
CREATE TABLE IF NOT EXISTS equipment (
 observation_id TEXT PRIMARY KEY REFERENCES observations, vri_id TEXT, facility_inn TEXT,
 verification_date TEXT, equipment_type TEXT, modification TEXT, serial_number TEXT,
 verification_result TEXT);
CREATE TABLE IF NOT EXISTS metrics (
 observation_id TEXT PRIMARY KEY REFERENCES observations, result TEXT NOT NULL);
DROP VIEW IF EXISTS graph_edges;
CREATE VIEW graph_edges AS
 SELECT c.customer_inn AS party_a, c.supplier_inn AS party_b, c.contract_id AS identifier,
 'contract' AS kind, e.observation_id, o.input_sha256,o.collected_at,o.source_url
 FROM contracts c JOIN contract_evidence e USING(customer_inn,supplier_inn,contract_id)
 JOIN observations o ON o.id=e.observation_id
 WHERE json_extract(o.payload, '$.is_eligible_for_calculation') = 1
 UNION ALL
 SELECT c.plaintiff_inn,c.respondent_inn,c.case_number,'court',c.observation_id,
 o.input_sha256,o.collected_at,o.source_url FROM court_evidence c
 JOIN observations o ON o.id=c.observation_id
 WHERE json_extract(o.payload, '$.is_eligible_for_calculation') = 1;
'''


def dumps(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False)


def utc(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('collected_at must include timezone')
    return parsed.astimezone(timezone.utc).isoformat()


def source_url(value):
    if value is None:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Invalid public source_url')
    return value


class Ledger:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def observe(self, kind, row, path, digest, line, meta, pilots):
        if kind not in KINDS:
            raise ValueError('Unsupported kind')
        row, _, sanitization_gaps = sanitize_record(row, path, meta)
        if row is None or 'synthetic_demo_or_test_origin' in sanitization_gaps:
            return None
        when = utc(row.get('collected_at') or row.get('source_fetched_at') or meta.get('collected_at'))
        url = row.get('source_url') or meta.get('source_url')
        if kind == 'trade' and not url and row.get('source_endpoint'):
            url = row['source_endpoint'] + '?' + urlencode(row.get('source_query', {}))
        url = source_url(url)
        gaps = list(sanitization_gaps)
        if when is None:
            gaps.append('missing_primary_collection_timestamp')
        if url is None:
            gaps.append('missing_primary_source_url')
        result = None
        edge = None
        if kind in ('contract', 'court'):
            a = row.get('customer_inn') if kind == 'contract' else row.get('plaintiff_inn')
            b = row.get('supplier_inn') if kind == 'contract' else row.get('respondent_inn')
            number = row.get('contract_id') if kind == 'contract' else row.get('case_number')
            if not pilots:
                gaps.append('approved_20_pilot_scope_missing')
            elif a not in pilots or b not in pilots:
                gaps.append('outside_mutual_pilot_scope')
            if not a or not b or a == b or not valid_inn(a) or not valid_inn(b):
                gaps.append('unverified_party_identifiers')
            if not isinstance(number, str) or not number.strip():
                gaps.append('missing_contract_or_case_number')
            if row.get('verified') is not True:
                gaps.append('unverified_contract_parties_or_opposing_court_roles')
            if not gaps:
                edge = (a, b, number.strip())
        elif kind == 'inventory':
            result = dict(tts_days=tts(row.get('S_safe'), row.get('D_daily'),
                          row.get('stock_unit'), row.get('demand_unit'), row.get('is_eligible_for_calculation') is True),
                          S_safe=row.get('S_safe'), D_daily=row.get('D_daily'))
            if result['tts_days'] is None:
                gaps.append('missing_or_incompatible_verified_stock_and_daily_demand')
        elif kind == 'consumption':
            if row.get('r_kj') is not None:
                nonnegative(row['r_kj'])
            result = dict(r_kj=row.get('r_kj') if row.get('is_eligible_for_calculation') is True and row.get('unit') else None,
                          unit=row.get('unit'))
            if result['r_kj'] is None:
                gaps.append('missing_verified_specific_consumption')
        elif kind == 'radar':
            result = radar_candidate(row.get('t_prev'), row.get('t_observed'))
            if result is None:
                gaps.append('missing_radar_observation_bounds')
        elif kind == 'scenario':
            # Scenario assumptions are archived, but never enter factual metrics.
            row['is_eligible_for_calculation'] = False
            gaps.append('scenario_excluded_from_factual_calculation')
        elif kind == 'trade':
            result = dict(metric='external_logistics_cycle', interval_basis=row.get('interval_basis'),
                          delta_t_days=row.get('delta_t_days'), tts_days=None,
                          establishes_firm_supply_edge=False)
        identifier = hashlib.sha256(dumps([kind, digest, line, row, when, url, sorted(pilots)]).encode()).hexdigest()
        self.db.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (identifier, kind, digest, when, url, datetime.now(timezone.utc).isoformat(),
                         str(path.resolve()), line, dumps(row), dumps(gaps)))
        if edge:
            a, b, number = edge
            for party in (a, b):
                self.db.execute('INSERT OR IGNORE INTO entities VALUES (?)', (party,))
            if kind == 'contract':
                self.db.execute('INSERT OR IGNORE INTO contracts VALUES (?,?,?)', edge)
                self.db.execute('INSERT OR IGNORE INTO contract_evidence VALUES (?,?,?,?)', (*edge, identifier))
            else:
                self.db.execute('INSERT OR IGNORE INTO court_evidence VALUES (?,?,?,?)', (*edge, identifier))
        if kind == 'trade':
            self.db.execute('INSERT OR IGNORE INTO monthly_trade VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (identifier, row.get('period'), row.get('reporter_code'), row.get('partner_code'),
                             row.get('hs_code'), row.get('flow_code'), row.get('trade_value_usd'),
                             row.get('net_weight_kg'), row.get('quantity'), row.get('quantity_unit')))
        if kind == 'equipment':
            self.db.execute('INSERT OR IGNORE INTO equipment VALUES (?,?,?,?,?,?,?,?)',
                            (identifier, row.get('vri_id'), row.get('facility_inn'), row.get('verification_date'),
                             row.get('equipment_type'), row.get('equipment_modification'),
                             row.get('equipment_serial_number'), row.get('verification_result')))
        if kind == 'inventory':
            self.db.execute('INSERT OR IGNORE INTO inventory VALUES (?,?,?,?,?,?,?)',
                            (identifier, row.get('inn'), row.get('S_safe'), row.get('D_daily'),
                             row.get('stock_unit'), row.get('demand_unit'), int(row.get('is_eligible_for_calculation') is True)))
        if kind == 'consumption':
            self.db.execute('INSERT OR IGNORE INTO consumption VALUES (?,?,?,?,?,?)',
                            (identifier, row.get('supplier_inn'), row.get('product_id'),
                             row.get('r_kj'), row.get('unit'), int(row.get('is_eligible_for_calculation') is True)))
        if result is not None:
            result['is_eligible_for_calculation'] = row.get('is_eligible_for_calculation') is True
            self.db.execute('INSERT OR IGNORE INTO metrics VALUES (?,?)', (identifier, dumps(result)))
        return identifier

    def report(self, output, run_id, pilots, pending):
        output.mkdir(parents=True, exist_ok=True)
        groups = {k: [] for k in CATEGORIES}
        for row in self.db.execute('SELECT * FROM query_runs WHERE run_id=? ORDER BY query_id', (run_id,)):
            groups[row['category']].append(dict(query_id=row['query_id'], source=row['source'],
                                               **json.loads(row['details'])))
        coverage = dict(run_id=run_id, generated_at=datetime.now(timezone.utc).isoformat(),
                        categories=groups, counts={k: len(v) for k, v in groups.items()},
                        scope='explicit query outcomes only; not inferred from record absence',
                        pending_queries=pending, query_scope_complete=not pending and bool(sum(map(len, groups.values()))))
        edges = [dict(r) for r in self.db.execute('SELECT e.* FROM graph_edges e JOIN run_observations r USING(observation_id) WHERE r.run_id=?', (run_id,))]
        metrics = [dict(observation_id=r[0], **json.loads(r[1])) for r in self.db.execute("SELECT m.* FROM metrics m JOIN run_observations r USING(observation_id) JOIN observations o ON o.id=m.observation_id WHERE r.run_id=? AND json_extract(o.payload, '$.is_eligible_for_calculation')=1 AND json_extract(m.result, '$.is_eligible_for_calculation')=1", (run_id,))]
        gaps = [dict(observation_id=r['id'], kind=r['kind'], gaps=json.loads(r['gaps']))
                for r in self.db.execute("SELECT o.id,o.kind,o.gaps FROM observations o JOIN run_observations r ON r.observation_id=o.id WHERE r.run_id=? AND o.gaps != '[]'", (run_id,))]
        kinds = {r[0] for r in self.db.execute('SELECT DISTINCT o.kind FROM observations o JOIN run_observations r ON r.observation_id=o.id WHERE r.run_id=?', (run_id,))}
        missing = [kind for kind in KINDS if kind not in kinds]
        audit = dict(generated_at=coverage['generated_at'], evidence_backed_edges=edges,
                     scenarios=[m for m in metrics if m.get('evidence_class') == 'scenario'],
                     observation_metrics=[m for m in metrics if m.get('evidence_class') != 'scenario'],
                     primary_observation_gaps=gaps, missing_observation_kinds=missing,
                     pilot_scope_complete=len(pilots) == 20, pilot_count=len(pilots),
                     pending_queries=pending,
                     myerson_value=None, myerson_gap='Explicit coalition value function, units and time window required',
                     interpretation='Court edges establish litigation, not supply direction; radar candidates do not prove operational recovery; trade aggregates do not establish firm links.')
        for name, data in (('coverage.json', coverage), ('evidence_limits.json', audit)):
            temporary = output / (name + '.tmp')
            temporary.write_text(dumps(data) + '\n', encoding='utf-8')
            temporary.replace(output / name)
        return coverage


def run(manifest_path, database, output):
    config = json.loads(manifest_path.read_text())
    pilots = config.get('pilot_inns', [])
    if pilots and (len(pilots) != 20 or len(set(pilots)) != 20 or not all(valid_inn(i) for i in pilots)):
        raise ValueError('Approved pilot_inns must contain exactly 20 distinct checksum-valid INNs')
    pilots = set(pilots)
    run_id = datetime.now(timezone.utc).isoformat()
    ledger = Ledger(database)
    try:
        with ledger.db:
            for item in config.get('inputs', []):
                path = (manifest_path.parent / item['path']).resolve()
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                rows = [(n, json.loads(line)) for n, line in enumerate(raw.decode('utf-8-sig').splitlines(), 1) if line.strip()]
                for line, row in rows:
                    identifier = ledger.observe(item['kind'], row, path, digest, line, item, pilots)
                    if identifier is not None:
                        ledger.db.execute('INSERT OR IGNORE INTO run_observations VALUES (?,?)', (run_id, identifier))
            queries = config.get('queries', [])
            if len({q['query_id'] for q in queries}) != len(queries):
                raise ValueError('Duplicate query_id')
            for q in queries:
                if q['category'] not in CATEGORIES:
                    raise ValueError('Unknown query category')
                ledger.db.execute('INSERT INTO query_runs VALUES (?,?,?,?,?)',
                                  (run_id, q['query_id'], q['source'], q['category'],
                                   dumps({k: v for k, v in q.items() if k not in ('query_id','source','category')})))
        return ledger.report(output, run_id, pilots, config.get('pending_queries', []))
    finally:
        ledger.db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--database', type=Path, default=Path('outputs/evidence_pipeline/evidence.sqlite3'))
    parser.add_argument('--output', type=Path, default=Path('outputs/evidence_pipeline'))
    args = parser.parse_args()
    print(dumps(run(args.manifest, args.database, args.output)))

"""Offline adapters for archived contract CSV and saved KAD case HTML."""
import csv
import hashlib
import itertools
import json
from pathlib import Path
import re
import sys

from bs4 import BeautifulSoup
from eis_ftp_contracts import date_iso, dump_record, rub_price, valid_inn


def nullable(text):
    return text.strip() or None if isinstance(text, str) else None


def inn_format(text):
    return bool(text and re.fullmatch(r'[0-9]{10}|[0-9]{12}', text))


def filter_csv(source, output, targets):
    csv.field_size_limit(sys.maxsize)
    output.mkdir(parents=True, exist_ok=True)
    matched = []; provenance = []; edges = {}; participants = set(); total = 0
    per_target = {i: {'customer_records': 0, 'supplier_records': 0} for i in sorted(targets)}
    digest = hashlib.sha256()
    with source.open('rb') as handle:
        for block in iter(lambda: handle.read(1024*1024), b''):
            digest.update(block)
    source_hash = digest.hexdigest()
    with source.open(encoding='utf-8-sig', newline='') as src:
        for line, row in enumerate(csv.DictReader(src), 2):
            total += 1
            customer = nullable(row.get('customer_inn'))
            suppliers = json.loads(row.get('supplier_inns') or '[]')
            names = json.loads(row.get('supplier_names') or '[]')
            if (not isinstance(suppliers, list) or not isinstance(names, list)
                    or any(not isinstance(i, str) for i in suppliers)):
                raise ValueError(f'Invalid supplier arrays at CSV row {line}')
            if customer:
                participants.add(customer)
            participants.update(i for i in suppliers if isinstance(i, str) and i)
            for target in targets:
                per_target[target]['customer_records'] += int(customer == target)
                per_target[target]['supplier_records'] += int(target in suppliers)
            if customer not in targets and not targets.intersection(suppliers):
                continue
            candidate = dict(row, csv_row=str(line))
            candidate['selection_status'] = 'missing_supplier_inn' if not suppliers else 'counterparty_validation_required'
            products = json.loads(row.get('product_names') or '[]')
            subject = '; '.join(dict.fromkeys(p for p in products if isinstance(p, str) and p.strip())) or nullable(row.get('title'))
            accepted = 0
            for supplier in dict.fromkeys(suppliers):
                if not inn_format(customer) or not inn_format(supplier):
                    continue
                if not ((customer in targets and valid_inn(supplier)) or
                        (supplier in targets and valid_inn(customer))):
                    continue
                # Parallel arrays cannot establish the mapping of multiple names.
                supplier_name = nullable(names[0]) if len(suppliers) == len(names) == 1 else None
                record = dict(customer_inn=customer, customer_name=nullable(row.get('customer_name')),
                              supplier_inn=supplier, supplier_name=supplier_name,
                              contract_id=nullable(row.get('contract_number')),
                              sign_date=date_iso(nullable(row.get('sign_date'))),
                              price_rub=rub_price(nullable(row.get('price_rub')), 'RUB'),
                              product_description=subject, input_sha256=source_hash,
                              collected_at=None, source_url=nullable(row.get('source_url')))
                serialized = dump_record(record)
                edges[serialized] = record
                provenance.append(dict(record_sha256=hashlib.sha256(serialized.encode()).hexdigest(),
                                       input_file=str(source.resolve()), input_sha256=source_hash,
                                       csv_row=line, collected_at=None, source_url=nullable(row.get('source_url'))))
                accepted += 1
            if accepted:
                candidate['selection_status'] = 'valid_counterparty'
            matched.append(candidate)
    (output/'contracts.ndjson').write_text(''.join(edges), encoding='utf-8')
    with (output/'matched_contract_candidates.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        fields = list(matched[0]) if matched else ['contract_number', 'customer_inn', 'supplier_inns', 'selection_status', 'csv_row']
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(matched)
    (output/'provenance.ndjson').write_text(''.join(json.dumps(p, ensure_ascii=False)+'\n' for p in provenance), encoding='utf-8')
    report = dict(source=str(source.resolve()), source_sha256=source_hash, source_records=total,
                  distinct_participant_inns=len(participants), target_matches=len(matched),
                  valid_contract_edges=len(edges), per_target=per_target,
                  incomplete_candidates=len([r for r in matched if r['selection_status'] != 'valid_counterparty']),
                  scope='local snapshot only; target customer OR target supplier; opposite party checksum required')
    return report


def parse_kad_html(path, pairs):
    from contract_links_async import CourtCase, court_evidence, parse_date
    raw = path.read_bytes()
    soup = BeautifulSoup(raw.decode('utf-8', errors='replace'), 'html.parser')
    section = soup.select_one('#case-participants, .case-participants, [data-role="participants"]')
    evidence = {'file': str(path.resolve()), 'sha256': hashlib.sha256(raw).hexdigest(),
                'status': 'unrecognized_participant_structure', 'cases': 0}
    if section is None:
        if soup.find(string=re.compile(r'По вашему запросу дел не найдено')):
            evidence['status'] = 'saved_empty_search_not_case'
        elif soup.select_one('table.b-cases'):
            evidence['status'] = 'saved_search_requires_case_cards'
        return [], evidence
    roles = {}
    for role in ('plaintiff', 'respondent'):
        node = section.select_one(f'[data-role="{role}"], .{role}, #{role}')
        roles[role] = set(re.findall(r'(?<!\d)(?:\d{12}|\d{10})(?!\d)', node.get_text(' ', strip=True))) if node else set()
    number_node = soup.select_one('[data-role="case-number"], .b-case-number, .case-number')
    number = nullable(number_node.get_text(' ', strip=True)) if number_node else None
    link = soup.select_one('link[rel="canonical"]')
    url = link.get('href') if link else None
    if not url or not re.fullmatch(r'https://kad\.arbitr\.ru/Card/[0-9a-fA-F-]{36}', url):
        evidence['status'] = 'missing_verified_case_url'
        return [], evidence
    date_node = soup.select_one('[data-role="registration-date"]')
    registered = parse_date(date_node.get_text(strip=True)) if date_node else None
    amount, dispute = court_evidence(soup.get_text('\n', strip=True))
    cases = []
    for a, b in sorted(pairs):
        directions = [(x, y) for x, y in ((a, b), (b, a)) if x in roles['plaintiff'] and y in roles['respondent']]
        if len(directions) != 1:
            continue
        cases.append(CourtCase(party_a_inn=a, party_b_inn=b, plaintiff_inn=directions[0][0],
                               respondent_inn=directions[0][1], case_number=number,
                               registration_date=registered, claim_amount_rub=amount,
                               dispute_text=dispute, source_url=url, evidence_urls=[url],
                               dispute_evidence='keyword_candidate' if dispute else 'not_available'))
    evidence.update(status='parsed_local_case', cases=len(cases))
    return cases, evidence


def run_local(args):
    from contract_links_async import atomic_ndjson
    targets = set(args.inns)
    report = filter_csv(args.local_contracts, args.output, targets)
    if args.kad:
        pairs = set(itertools.combinations(sorted(targets), 2))
        if args.pairs:
            for pair in json.loads(args.pairs.read_text()):
                if len(pair) != 2 or pair[0] == pair[1] or not all(inn_format(i) for i in pair):
                    raise ValueError('Each pair requires two distinct INNs')
                pairs.add(tuple(sorted(pair)))
        cases = []; evidence = []
        for path in args.kad_html or []:
            parsed, audit = parse_kad_html(path, pairs)
            cases.extend(parsed); evidence.append(audit)
        atomic_ndjson(args.output/'court_cases.ndjson', cases)
        queue = [{'party_a_inn': a, 'party_b_inn': b,
                  'status': 'local_case_found' if any(c.party_a_inn == a and c.party_b_inn == b for c in cases) else 'needs_saved_search_and_case_html'}
                 for a, b in sorted(pairs)]
        (args.output/'kad_pair_queue.json').write_text(json.dumps(queue, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        report['kad'] = dict(mode='local_html_only', requested_pairs=len(pairs), cases=len(cases),
                             inputs=evidence, search_coverage_complete=False,
                             note='Search rows alone do not establish opposing INNs or a contract; case HTML must prove procedural roles.')
    (args.output/'coverage.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0

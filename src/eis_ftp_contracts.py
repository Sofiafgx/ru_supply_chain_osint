"""Stream EIS contract ZIPs to NDJSON (Python 3.9+, standard library only)."""
import argparse
import asyncio
import contextlib
import datetime as dt
import decimal
import ftplib
import json
import logging
import os
from pathlib import Path
import posixpath
import re
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile

TARGETS = {'1646019864', '2128700223', '1804002498', '6612001460',
           '7719830028', '6444009038'}
LOG = logging.getLogger(__name__)
RECORD_TAGS = {'contract', 'fcsContract', 'contractInfo'}

# Paths use local names, independent of namespace prefix/version. Extend these
# with --paths-json for a verified XSD version; never search all INNs globally.
PATHS = {
    '44': {
        'customer': ['customer'],
        'supplier': ['suppliers/supplier'],
        'id': ['regNum', 'registryNumber', 'id'],
        'date': ['signDate'],
        'price': ['price', 'priceInfo/price'],
        'currency': ['currency/code', 'currencyCode', 'priceInfo/currency/code'],
        'products': ['products/product/name', 'products/product/productName',
                     'products/product/description', 'subject'],
    },
    '223': {
        'customer': ['body/item/customerInfo', 'body/item/customer', 'body/item/contractData/customerInfo',
                     'body/item/contractData/customer', 'customerInfo', 'customer'],
        'supplier': ['body/item/contractData/supplierInfo/supplier', 'body/item/contractData/supplierInfo',
                     'body/item/contractData/suppliers/supplier',
                     'contractData/supplierInfo', 'supplierInfo',
                     'suppliers/supplier', 'winner'],
        'id': ['body/item/contractData/contractRegNumber', 'body/item/registrationNumber',
               'body/item/contractData/registrationNumber', 'contractRegNumber', 'registrationNumber', 'regNum'],
        'date': ['body/item/contractData/contractDate', 'body/item/contractData/signDate',
                 'contractData/contractDate', 'contractDate', 'signDate'],
        'price': ['body/item/contractData/price', 'body/item/contractData/contractPrice',
                  'contractData/price', 'contractPrice', 'price'],
        'currency': ['body/item/contractData/currency/code',
                     'body/item/contractData/currencyCode', 'currency/code', 'currencyCode'],
        'products': ['body/item/contractData/contractSubject',
                     'body/item/contractData/contractPositions/contractPosition/name',
                     'body/item/contractData/contractPositions/contractPosition/productName',
                     'body/item/contractData/contractPositions/contractPosition/description',
                     'contractSubject', 'products/product/name'],
    },
}


def local(tag):
    return tag.rsplit('}', 1)[-1]


def nodes(root, path):
    current = [root]
    for part in path.split('/'):
        current = [c for n in current for c in n if local(c.tag) == part]
    return current


def value(node):
    if node is None:
        return None
    text = ' '.join(''.join(node.itertext()).split())
    return text or None


def first(root, paths):
    for path in paths:
        for node in nodes(root, path):
            text = value(node)
            if text is not None:
                return text
    return None


def valid_inn(text):
    if not text or not re.fullmatch(r'[0-9]{10}|[0-9]{12}', text):
        return None
    digits = list(map(int, text))
    def check(weights, position):
        return sum(a*b for a, b in zip(digits, weights)) % 11 % 10 == digits[position]
    ok = (check([2, 4, 10, 3, 5, 9, 4, 6, 8], 9) if len(text) == 10 else
          check([7, 2, 4, 10, 3, 5, 9, 4, 6, 8], 10) and
          check([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8], 11))
    return text if ok and any(digits) else None


def party(node):
    # An organization's nested legal-entity wrapper is supported, but not an
    # unrelated participant elsewhere in the document.
    inn = next((value(n) for n in node.iter()
                if local(n.tag).lower() == 'inn' and value(n)
                and re.fullmatch(r'[0-9]{10}|[0-9]{12}', value(n))), None)
    name = next((value(n) for key in ('fullName', 'organizationName', 'name', 'shortName')
                 for n in node.iter() if local(n.tag) == key and value(n)), None)
    return inn, name


def date_iso(text):
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def rub_price(text, currency):
    # Never label foreign-currency amounts as RUB. 44-FZ price without an
    # explicit currency is the documented RUB field; 223-FZ requires currency.
    if not text or currency not in ('RUB', 'RUR', '643'):
        return None
    try:
        amount = decimal.Decimal(text.replace('\xa0', '').replace(' ', '').replace(',', '.'))
        return amount if amount.is_finite() and amount >= 0 else None
    except decimal.InvalidOperation:
        return None


def extract(root, law, targets, paths):
    mapping = paths[law]
    customer_nodes = next((found for p in mapping['customer'] if (found := nodes(root, p))), [])
    if not customer_nodes:
        return
    customer_inn, customer_name = party(customer_nodes[0])
    if customer_inn is None:
        return
    supplier_nodes = next((found for p in mapping['supplier'] if (found := nodes(root, p))), [])
    currency = first(root, mapping['currency'])
    if law == '44' and currency is None:
        currency = 'RUB'
    descriptions = list(dict.fromkeys(text for p in mapping['products']
                                      for n in nodes(root, p) if (text := value(n))))
    shared = {
        'customer_inn': customer_inn, 'customer_name': customer_name,
        'contract_id': first(root, mapping['id']),
        'sign_date': date_iso(first(root, mapping['date'])),
        'price_rub': rub_price(first(root, mapping['price']), currency),
        'product_description': '; '.join(descriptions) or None,
    }
    seen = set()
    for supplier_node in supplier_nodes:
        supplier_inn, supplier_name = party(supplier_node)
        if supplier_inn is None or supplier_inn in seen:
            continue
        seen.add(supplier_inn)
        if ((customer_inn in targets and valid_inn(supplier_inn)) or
                (supplier_inn in targets and valid_inn(customer_inn))):
            yield {**shared, 'supplier_inn': supplier_inn, 'supplier_name': supplier_name}


class SafeXML:
    """Reject DTD/entity declarations, including declarations across read blocks."""
    def __init__(self, stream):
        self.stream, self.tail = stream, b''

    def read(self, size=-1):
        data = self.stream.read(size)
        scan = (self.tail + data).replace(b'\x00', b'').upper()
        if b'<!DOCTYPE' in scan or b'<!ENTITY' in scan:
            raise ValueError('DTD/entity declarations are not accepted')
        self.tail = (self.tail + data)[-64:]
        return data


def xml_records(stream, law, targets, paths):
    stack = []
    active = None
    saw_record = False
    for event, elem in ET.iterparse(SafeXML(stream), events=('start', 'end')):
        if event == 'start':
            stack.append(elem)
            if active is None and local(elem.tag) in RECORD_TAGS:
                active = elem
                saw_record = True
            continue
        if elem is active:
            items = nodes(elem, 'body/item') if law == '223' else []
            if len(items) > 1:
                for item in items:
                    record = ET.Element('contract')
                    ET.SubElement(record, 'body').append(item)
                    yield from extract(record, law, targets, paths)
            else:
                yield from extract(elem, law, targets, paths)
            active = None
        if active is None:
            elem.clear()
            if len(stack) > 1:
                stack[-2].remove(elem)
        stack.pop()
    if not saw_record:
        raise ValueError('No supported contract record nodes; verify the XML schema')


def dump_record(record):
    # Serialize Decimal as an exact JSON number, without float rounding.
    return '{' + ','.join(json.dumps(k) + ':' +
                         (format(v, 'f') if isinstance(v, decimal.Decimal) else
                          json.dumps(v, ensure_ascii=False, allow_nan=False))
                         for k, v in record.items()) + '}\n'


def parse_zip(archive, law, output, targets, paths, max_xml_bytes):
    count = 0
    with zipfile.ZipFile(archive) as zf, open(output, 'w', encoding='utf-8') as out:
        members = [m for m in zf.infolist() if not m.is_dir() and m.filename.lower().endswith('.xml')]
        if not members:
            raise ValueError(f'No XML members in {archive}')
        for member in members:
            if member.file_size > max_xml_bytes:
                raise ValueError(f'XML exceeds --max-xml-mib: {member.filename}')
            with zf.open(member) as stream:
                for record in xml_records(stream, law, targets, paths):
                    out.write(dump_record(record))
                    count += 1
    return count


@contextlib.contextmanager
def ftp_session(args):
    ftp = ftplib.FTP(timeout=args.timeout)
    try:
        ftp.connect(args.host, args.port)
        ftp.login('anonymous', 'anonymous')
        ftp.set_pasv(True)
        yield ftp
    finally:
        ftp.close()


def listing(ftp, directory):
    try:
        return [(name, facts.get('type'), facts.get('modify', ''))
                for name, facts in ftp.mlsd(directory) if name not in ('.', '..')]
    except ftplib.error_perm:
        # Older FTP servers have no MLSD. Check directories with CWD.
        result = []
        previous = ftp.pwd()
        for raw in ftp.nlst(directory):
            name = posixpath.basename(raw.rstrip('/'))
            if name in ('.', '..'):
                continue
            try:
                ftp.cwd(posixpath.join(directory, name))
                kind = 'dir'
            except ftplib.error_perm:
                kind = 'file'
            finally:
                ftp.cwd(previous)
            result.append((name, kind, ''))
        return result


def archive_day(name, modified):
    timestamps = re.findall(r'(?<!\d)(20\d{6})(\d{2}(?:\d{4})?)?(?!\d)', name)
    if timestamps:
        try:
            dates = [dt.datetime.strptime(day, '%Y%m%d').date() for day, _ in timestamps]
        except ValueError:
            return None
        # Exclude monthly/yearly packages. Keep all parts of a daily export.
        if max(dates) - min(dates) > dt.timedelta(days=1):
            return None
        return min(dates)
    if modified:
        try:
            return dt.datetime.strptime(modified[:8], '%Y%m%d').date()
        except ValueError:
            pass
    return None


def discover(args):
    jobs = []
    with ftp_session(args) as ftp:
        for law in args.laws:
            root = args.root44 if law == '44' else args.root223
            regions = args.regions or [name for name, kind, _ in listing(ftp, root) if kind == 'dir']
            if not regions:
                raise ValueError(f'No region directories at {root}; set --root{law}/--remote-dir')
            for region in regions:
                candidates = []
                base = posixpath.join(root, region)
                # Explicit contract-only directories, never a generic recursive
                # scan of notices, protocols or other document types.
                directories = [posixpath.join(base, rel) for rel in args.remote_dirs]
                for directory in directories:
                    try:
                        entries = listing(ftp, directory)
                    except ftplib.error_perm as exc:
                        if str(exc).startswith('550'):
                            continue
                        raise
                    for name, kind, modified in entries:
                        if kind == 'file' and re.fullmatch(r'contract_.*\.xml\.zip', name, re.I):
                            day = archive_day(name, modified)
                            if day is not None:
                                candidates.append((day, posixpath.join(directory, name)))
                if not candidates:
                    LOG.warning('%s / %s: no daily contract archives found', law, region)
                    continue
                days = set(sorted({day for day, _ in candidates}, reverse=True)[:args.days])
                selected = sorted({path for day, path in candidates if day in days})
                LOG.info('%s / %s: %d archives, days %s', law, region, len(selected), sorted(days))
                jobs.extend((law, path) for path in selected)
    if not jobs:
        raise ValueError('No daily contract archives found; verify FTP access and directory layout')
    return jobs


def process_job(job, args, paths, tempdir):
    law, source = job
    with tempfile.TemporaryDirectory(dir=tempdir) as work:
        archive = Path(work) / 'archive.zip'
        if args.local_zip:
            archive = Path(source)
        else:
            for attempt in range(args.retries + 1):
                try:
                    with ftp_session(args) as ftp, archive.open('wb') as out:
                        total = 0
                        def receive(block):
                            nonlocal total
                            total += len(block)
                            if total > args.max_zip_mib * 1024**2:
                                raise ValueError('Archive exceeds --max-zip-mib')
                            out.write(block)
                        ftp.retrbinary('RETR ' + source, receive, blocksize=256*1024)
                    break
                except (OSError, ftplib.Error, EOFError):
                    if attempt == args.retries:
                        raise
                    time.sleep(min(2**attempt, 8))
        fd, spool = tempfile.mkstemp(suffix='.ndjson', dir=tempdir)
        os.close(fd)
        try:
            count = parse_zip(archive, law, spool, args.targets, paths, args.max_xml_mib * 1024**2)
        except Exception:
            Path(spool).unlink(missing_ok=True)
            raise
        return spool, count


async def run(args, paths):
    jobs = ([(args.local_law, str(p)) for p in args.local_zip] if args.local_zip else
            await asyncio.to_thread(discover, args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Entire result is replaced only after every archive succeeds.
    with tempfile.TemporaryDirectory(dir=args.temp_dir) as work:
        fd, staged = tempfile.mkstemp(prefix='.eis-', dir=args.output.parent)
        os.close(fd)
        queue = asyncio.Queue()
        for job in jobs:
            queue.put_nowait(job)
        lock = asyncio.Lock()
        total = 0
        def append(spool):
            with open(staged, 'ab') as dst, open(spool, 'rb') as src:
                shutil.copyfileobj(src, dst, 1024*1024)
            Path(spool).unlink()
        async def worker():
            nonlocal total
            while not queue.empty():
                job = queue.get_nowait()
                spool, count = await asyncio.to_thread(process_job, job, args, paths, work)
                async with lock:
                    await asyncio.to_thread(append, spool)
                    total += count
                LOG.info('%s: %d matching pairs', job[1], count)
        tasks = [asyncio.create_task(worker()) for _ in range(min(args.concurrency, len(jobs)))]
        try:
            # Wait for running thread jobs even on failure, before deleting work.
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failures = [r for r in results if isinstance(r, BaseException)]
            if failures:
                raise failures[0]
            os.replace(staged, args.output)
        finally:
            Path(staged).unlink(missing_ok=True)
    LOG.info('Saved %d pairs to %s', total, args.output)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', default='ftp.zakupki.gov.ru')
    p.add_argument('--port', type=int, default=21)
    p.add_argument('--root44', default='/fcs_regions')
    p.add_argument('--root223', default='/dish', help='Verify layout; historical 223 exports may use /out/published')
    p.add_argument('--laws', nargs='+', choices=['44', '223'], default=['44', '223'])
    p.add_argument('--regions', nargs='+', help='Exact FTP region names; omitted = all regions')
    p.add_argument('--remote-dir', dest='remote_dirs', action='append',
                   help='Region-relative contract directory, repeatable')
    p.add_argument('--days', type=int, default=1, help='Latest available daily export dates per region/law')
    p.add_argument('--inn', dest='targets', nargs='+', default=sorted(TARGETS))
    p.add_argument('--output', type=Path, default=Path('contracts.ndjson'))
    p.add_argument('--concurrency', type=int, default=3)
    p.add_argument('--timeout', type=float, default=30)
    p.add_argument('--retries', type=int, default=2)
    p.add_argument('--temp-dir', type=Path)
    p.add_argument('--max-zip-mib', type=int, default=4096)
    p.add_argument('--max-xml-mib', type=int, default=1024)
    p.add_argument('--local-zip', nargs='+', type=Path)
    p.add_argument('--local-law', choices=['44', '223'], default='44')
    p.add_argument('--paths-json', type=Path, help='Override mapping fields for verified XML schema paths')
    args = p.parse_args()
    if min(args.days, args.concurrency, args.max_zip_mib, args.max_xml_mib, args.timeout) <= 0 or args.retries < 0:
        p.error('Limits must be positive, retries must be nonnegative')
    if any(not re.fullmatch(r'[0-9]{10}|[0-9]{12}', t) for t in args.targets):
        p.error('Target INNs must contain 10 or 12 digits')
    args.targets = set(args.targets)
    args.remote_dirs = args.remote_dirs or ['contracts/currMonth', 'contracts', 'contract/daily', 'contract']
    if any(posixpath.isabs(d) or '..' in d.split('/') for d in args.remote_dirs):
        p.error('--remote-dir must be relative to a region directory')
    if args.regions and any('/' in r or r in ('.', '..') for r in args.regions):
        p.error('--regions must contain directory names')
    paths = {law: dict(mapping) for law, mapping in PATHS.items()}
    if args.paths_json:
        overrides = json.loads(args.paths_json.read_text(encoding='utf-8'))
        for law, mapping in overrides.items():
            if law not in paths or not isinstance(mapping, dict):
                p.error('Mapping must contain law keys 44/223 and field dictionaries')
            for field, values in mapping.items():
                if field not in paths[law] or not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                    p.error('Mapping fields must be lists of XML paths')
                paths[law][field] = values
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    for target in sorted(args.targets):
        if not valid_inn(target):
            LOG.warning('Target %s fails checksum; retained exactly as supplied; opposite party must be valid', target)
    try:
        asyncio.run(run(args, paths))
    except (OSError, ftplib.Error, EOFError, ValueError, ET.ParseError, zipfile.BadZipFile) as exc:
        LOG.error('%s: %s; result was not replaced', type(exc).__name__, exc)
        raise SystemExit(1)


if __name__ == '__main__':
    main()

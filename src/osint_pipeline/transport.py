"""Rotating, bounded HTTP transport and a content-addressed evidence archive."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import itertools
import json
from pathlib import Path
import random
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx


class CollectionError(ValueError):
    pass


class Deferred(CollectionError):
    def __init__(self, seconds):
        self.retry_after_seconds = seconds
        super().__init__('server_retry_after_exceeds_wait_budget')


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def public_url(url, secrets=()):
    parts = urlsplit(str(url))
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise CollectionError('invalid_source_url')
    sensitive = {'key', 'api_key', 'apikey', 'token', 'access_token', 'map_key', 'subscription-key'}
    query = [(k, '[REDACTED]' if k.lower() in sensitive else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    result = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))
    for secret in secrets:
        if secret:
            result = result.replace(str(secret), '[REDACTED]')
            result = result.replace(quote(str(secret), safe=''), '[REDACTED]')
    return result


class Archive:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, raw, source_url, collected_at, **metadata):
        digest = hashlib.sha256(raw).hexdigest()
        path = self.root / (digest + '.bin')
        if not path.exists():
            path.write_bytes(raw)
        evidence = dict(source_url=source_url, input_sha256=digest, collected_at=collected_at,
                        raw_file=str(path.resolve()), **metadata)
        with (self.root / 'responses.ndjson').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(evidence, ensure_ascii=False, allow_nan=False) + '\n')
            stream.flush()
        return evidence


def retry_after(value):
    try:
        return max(0.0, float(value))
    except (ValueError, TypeError):
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0.0


class Transport:
    def __init__(self, archive, *, proxies=(), timeout=50, attempts=4, interval=1,
                 max_wait=60, factory=httpx.AsyncClient, sleep=asyncio.sleep):
        if not 0 < timeout <= 50 or not 1 <= attempts <= 10 or interval < 0 or not 0 <= max_wait <= 60:
            raise ValueError('Invalid network limits (read timeout <=50, attempts 1..10, max_wait <=60)')
        self.proxies = list(dict.fromkeys(proxies)) or [None]
        for proxy in self.proxies:
            if proxy is not None:
                p = urlsplit(proxy)
                if p.scheme not in ('http', 'https', 'socks5') or not p.hostname or not p.port:
                    raise ValueError('Proxy requires http/https/socks5 URL and port')
        self.archive, self.timeout, self.attempts = archive, timeout, attempts
        self.interval, self.max_wait, self.factory, self.sleep = interval, max_wait, factory, sleep
        self.stack = AsyncExitStack()
        self.next_time = {}
        self.rotation = itertools.cycle(range(len(self.proxies)))

    async def __aenter__(self):
        self.clients = []
        try:
            for proxy in self.proxies:
                client = self.factory(proxy=proxy, trust_env=False, verify=True, follow_redirects=False,
                                      timeout=httpx.Timeout(self.timeout, connect=min(15, self.timeout)),
                                      headers={'User-Agent': 'SupplyChainOSINT/2.0 (public-source research)'})
                self.clients.append(await self.stack.enter_async_context(client))
        except BaseException:
            await self.stack.aclose()
            raise
        return self

    async def __aexit__(self, *args):
        await self.stack.aclose()

    async def request(self, method, url, *, secrets=(), **kwargs):
        public_url(url, secrets)
        # Same proxy session for redirects; a retry rotates to the next proxy.
        for attempt in range(self.attempts):
            client = self.clients[next(self.rotation)]
            host = urlsplit(url).hostname
            delay = max(0, self.next_time.get(host, 0) - time.monotonic())
            await self.sleep(delay)
            self.next_time[host] = time.monotonic() + self.interval
            try:
                response = await client.request(method, url, **kwargs)
                for _ in range(6):
                    evidence = self.archive.save(response.content, public_url(response.url, secrets), now(),
                                                 status_code=response.status_code, method=response.request.method)
                    response.extensions['evidence'] = evidence
                    if not response.is_redirect:
                        break
                    # Rebuild a GET on same-host redirects only; avoid forwarding credentials.
                    target = response.headers.get('location')
                    if not target:
                        raise CollectionError('redirect_without_location')
                    from urllib.parse import urljoin
                    target = urljoin(str(response.url), target)
                    public_url(target, secrets)
                    if urlsplit(target).hostname != urlsplit(url).hostname:
                        raise CollectionError('cross_host_redirect_requires_explicit_job_url')
                    if urlsplit(url).scheme == 'https' and urlsplit(target).scheme != 'https':
                        raise CollectionError('insecure_redirect')
                    if response.status_code in (307, 308):
                        response = await client.request(method, target, **{k: v for k, v in kwargs.items() if k != 'params'})
                    else:
                        response = await client.request('GET', target)
                else:
                    raise CollectionError('redirect_limit')
            except httpx.TransportError as exc:
                if attempt + 1 == self.attempts:
                    raise CollectionError('network_retries_exhausted:' + type(exc).__name__) from None
                await self.sleep(min(self.max_wait, 2 ** attempt + random.random()))
                continue
            if response.status_code in (429, 499) or 500 <= response.status_code <= 599:
                if attempt + 1 == self.attempts:
                    raise CollectionError('http_retries_exhausted:' + str(response.status_code))
                delay = max(retry_after(response.headers.get('Retry-After')), 2 ** attempt + random.random())
                if delay > self.max_wait:
                    raise Deferred(delay)
                await self.sleep(delay)
                continue
            if not response.is_success:
                raise CollectionError('http_status:' + str(response.status_code))
            return response
        raise CollectionError('retries_exhausted')

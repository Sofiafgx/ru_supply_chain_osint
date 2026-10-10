"""Bounded proxy rotation for explicitly requested public-source collection."""
from pathlib import Path
from urllib.parse import quote, urlsplit

from parsers.source_common import SourceClient, SourceError


def normalize_proxy(value):
    value = value.strip()
    if "://" not in value:
        parts = value.split(":")
        if len(parts) == 4:
            host, port, user, password = parts
            value = f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
        elif len(parts) == 2:
            value = "http://" + value
        else:
            raise ValueError("Unsupported proxy format")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https", "socks5", "socks5h") or not parsed.hostname or not parsed.port:
        raise ValueError("Invalid proxy address")
    return value


class RotatingSourceClient:
    """Maintain one session per route; retain TLS validation and redact credentials."""
    def __init__(self, proxy_file, *, offset=0, attempts=4, interval=1, timeout=20):
        lines = Path(proxy_file).read_text().splitlines()
        self.proxies = list(dict.fromkeys(normalize_proxy(x) for x in lines if x.strip() and not x.lstrip().startswith("#")))
        if not self.proxies:
            raise SourceError("Proxy file is empty")
        self.offset = offset % len(self.proxies)
        self.attempts = min(attempts, len(self.proxies))
        self.interval, self.timeout = interval, timeout
        self.sessions = {}
        self.failed_by_host = {}
        self.audit = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        for client in self.sessions.values():
            await client.__aexit__(*args)

    async def request(self, method, url, **kwargs):
        host = urlsplit(url).hostname
        failed = self.failed_by_host.setdefault(host, set())
        indices = [(self.offset+i) % len(self.proxies) for i in range(self.attempts)]
        for idx in indices:
            if idx in failed:
                continue
            if idx not in self.sessions:
                self.sessions[idx] = SourceClient(proxy=self.proxies[idx], interval=self.interval, timeout=self.timeout, retries=0)
            try:
                response = await self.sessions[idx].request(method, url, **kwargs)
                self.audit.append({"host": host, "route": f"proxy_{idx}", "status": response.status_code})
                return response
            except SourceError as exc:
                # SourceClient emits status/type only, without proxy credentials.
                self.audit.append({"host": host, "route": f"proxy_{idx}", "error": str(exc)})
                failed.add(idx)
        raise SourceError("No available proxy route within the configured attempt limit")

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from parsers.industrial_transport import normalize_proxy, RotatingSourceClient
from parsers.source_common import SourceError


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def test_proxy_formats(self):
        self.assertEqual(normalize_proxy("127.0.0.1:8080"), "http://127.0.0.1:8080")
        self.assertEqual(normalize_proxy("127.0.0.1:8080:test:p@ss"), "http://test:p%40ss@127.0.0.1:8080")
        with self.assertRaises(ValueError):
            normalize_proxy("not-a-proxy")

    async def test_failover_and_no_credentials_in_audit(self):
        with TemporaryDirectory() as d:
            p = Path(d)/"proxies.txt"
            p.write_text("http://user:secret@127.0.0.1:8080\nhttp://user:secret@127.0.0.2:8080\n")
            first, second = AsyncMock(), AsyncMock()
            first.request.side_effect = SourceError("HTTP 403")
            second.request.return_value.status_code = 200
            with patch("parsers.industrial_transport.SourceClient", side_effect=[first, second]):
                async with RotatingSourceClient(p, attempts=2) as client:
                    r = await client.request("GET", "https://joblab.ru/vacancy/1")
                    self.assertEqual(r.status_code, 200)
                    self.assertNotIn("secret", str(client.audit))
                    await client.request("GET", "https://joblab.ru/vacancy/2")
                    self.assertEqual(first.request.call_count, 1)
                    self.assertEqual(second.request.call_count, 2)


if __name__ == "__main__":
    unittest.main()

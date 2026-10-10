"""Append-only raw HTTP evidence, with exact bytes and UTC collection time."""
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def archive_response(path, response):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    audit = dict(input_sha256=hashlib.sha256(response.content).hexdigest(),
                 collected_at=datetime.now(timezone.utc).isoformat(),
                 source_url=str(response.request.url), http_status=response.status_code,
                 body_base64=base64.b64encode(response.content).decode('ascii'))
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(audit, ensure_ascii=False) + '\n')
        handle.flush()
    return {k: audit[k] for k in ('input_sha256', 'collected_at', 'source_url')}

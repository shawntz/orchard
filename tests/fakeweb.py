"""Offline stand-in for orchard.providers.Transport: regex URL routes, Range support, HTTP errors."""
import io
import json
import re
from urllib.error import HTTPError


class Reply:
    def __init__(self, body=b'', status=200, headers=None, final=None):
        self.body = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.status, self.headers, self.final = status, dict(headers or {}), final


class FakeWeb:
    def __init__(self, routes=None):
        self.routes = []  # (compiled regex, Reply or callable(url, headers) -> Reply)
        self.calls = []
        for pattern, reply in (routes or {}).items():
            self.add(pattern, reply)

    def add(self, pattern, reply):
        self.routes.insert(0, (re.compile(pattern), reply))
        return self

    def exact(self, url, reply):
        return self.add('^' + re.escape(url) + '$', reply)

    def match(self, url, headers):
        for pattern, reply in self.routes:
            if pattern.search(url):
                reply = reply(url, headers) if callable(reply) else reply
                return reply if isinstance(reply, Reply) else Reply(reply)
        return None

    def get(self, url, limit=8_000_000, callback=None, headers=None, truncate=False):
        headers = headers or {}
        self.calls.append((url, headers.get('Range')))
        reply = self.match(url, headers)
        if reply is None:
            raise HTTPError(url, 404, 'Not Found', {}, io.BytesIO(b'not found'))
        if reply.status >= 400:
            raise HTTPError(url, reply.status, 'error', reply.headers, io.BytesIO(reply.body))
        body, out = reply.body, dict(reply.headers)
        rng = re.fullmatch(r'bytes=(\d*)-(\d*)', headers.get('Range', ''))
        if rng and not out.pop('ignore-range', None):
            if rng.group(1) == '':
                start, end = max(0, len(body) - int(rng.group(2))), len(body) - 1
            else:
                start, end = int(rng.group(1)), min(len(body) - 1, int(rng.group(2) or len(body) - 1))
            out['Content-Range'] = f'bytes {start}-{end}/{len(body)}'
            body = body[start:end + 1]
        if len(body) > limit:
            if not truncate:
                raise ValueError('Response exceeds safe size limit.')
            body = body[:limit]
        return body, reply.final or url, out

    def requested(self, fragment):
        return [u for u, _ in self.calls if fragment in u]

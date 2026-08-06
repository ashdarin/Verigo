from __future__ import annotations

import io
import sys
from email.message import Message
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import safe_http


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int, url: str, headers: dict[str, str] | None = None):
        super().__init__(body)
        self.status = status
        self._url = url
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def geturl(self) -> str:
        return self._url

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class FakeOpener:
    def __init__(self):
        self.calls: list[str] = []

    def open(self, request, timeout):
        self.calls.append(request.full_url)
        if len(self.calls) == 1:
            from urllib.error import HTTPError
            redirect_headers = Message()
            redirect_headers["Location"] = "https://www.example.test/"
            error = HTTPError(request.full_url, 302, "redirect", redirect_headers, io.BytesIO())
            error.geturl = lambda: request.full_url  # type: ignore[method-assign]
            raise error
        return FakeResponse(b"ok", 200, request.full_url)


original_opener = safe_http._OPENER
original_public = safe_http.has_only_public_addresses
try:
    fake = FakeOpener()
    safe_http._OPENER = fake
    safe_http.has_only_public_addresses = lambda _host, _port=443: True
    response = safe_http.safe_fetch(
        "https://example.test/", allowed_hosts={"example.test", "www.example.test"}, max_bytes=10,
    )
    assert response is not None and response.body == b"ok"
    assert fake.calls == ["https://example.test/", "https://www.example.test/"]
finally:
    safe_http._OPENER = original_opener
    safe_http.has_only_public_addresses = original_public

print("safe http smoke: ok")

"""Small, bounded HTTP client for user-influenced domain discovery."""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


USER_AGENT = "VerigoDomainPreview/1.0"
DEFAULT_MAX_BYTES = 220_000


@dataclass(frozen=True)
class SafeResponse:
    status: int
    url: str
    body: bytes


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None


_OPENER = build_opener(_NoRedirect)


def normalize_host(value: str) -> str:
    return value.strip().lower().rstrip(".")


def public_addresses(host: str, port: int = 443) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    host = normalize_host(host)
    if not host:
        return set()
    try:
        return {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError):
        return set()


def has_only_public_addresses(host: str, port: int = 443) -> bool:
    addresses = public_addresses(host, port)
    return bool(addresses) and all(address.is_global for address in addresses)


def safe_fetch(
    url: str,
    *,
    timeout: float = 3.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allowed_hosts: set[str] | frozenset[str] | None = None,
    max_redirects: int = 2,
    headers: dict[str, str] | None = None,
) -> SafeResponse | None:
    """Fetch a public URL without following redirects into another trust zone."""
    current = url
    initial = urlsplit(url)
    initial_host = normalize_host(initial.hostname or "")
    if initial.scheme.lower() not in {"http", "https"} or not initial_host:
        return None
    allowed = {normalize_host(host) for host in (allowed_hosts or {initial_host})}
    for _ in range(max_redirects + 1):
        parsed = urlsplit(current)
        host = normalize_host(parsed.hostname or "")
        if parsed.scheme.lower() not in {"http", "https"} or not host or host not in allowed:
            return None
        if not has_only_public_addresses(host, parsed.port or (443 if parsed.scheme.lower() == "https" else 80)):
            return None
        request_headers = {"User-Agent": USER_AGENT}
        if headers:
            request_headers.update(headers)
        try:
            response = _OPENER.open(Request(current, headers=request_headers), timeout=timeout)
        except HTTPError as error:
            # With redirects disabled, urllib exposes 30x responses as errors.
            if error.code not in {301, 302, 303, 307, 308}:
                return None
            response = error
        try:
            status = int(getattr(response, "status", None) or response.getcode())
            location = response.headers.get("Location")
            if status in {301, 302, 303, 307, 308} and location:
                current = urljoin(current, location)
                continue
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                return None
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                return None
            return SafeResponse(status=status, url=response.geturl() or current, body=body)
        except (OSError, ValueError, TimeoutError):
            return None
    return None

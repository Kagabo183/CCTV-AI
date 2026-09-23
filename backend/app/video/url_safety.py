"""SSRF-safe URL validation and fetching.

The server downloads user-supplied video URLs (to hand them to the analyzer),
which is a classic SSRF vector. Defences:

1. Only http(s); http only when explicitly allowed. No credentials in URLs,
   only standard ports.
2. Optional domain allowlist (VIDEO_URL_ALLOWED_DOMAINS).
3. Every address a host resolves to must be public (no loopback, private,
   link-local incl. cloud metadata 169.254.169.254, CGNAT, multicast...).
4. The check is enforced *at connect time* by a custom network backend, and
   the connection is made to the validated IP. This closes DNS-rebinding
   gaps between "validate" and "fetch". TLS still verifies the real hostname.
5. Redirects are followed manually and each hop is re-validated.
6. Proxies from the environment are ignored; downloads are size-capped.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

from app.core.errors import ValidationFailed

_ALLOWED_PORTS = {None, 80, 443, 8080, 8443}
_MAX_REDIRECTS = 5
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


class UnsafeUrlError(ValidationFailed):
    code = "unsafe_url"


@dataclass(frozen=True)
class UrlPolicy:
    allowed_domains: tuple[str, ...] = ()
    allow_http: bool = False


def _host_allowed(host: str, allowed_domains: Iterable[str]) -> bool:
    allowed = [d.lower().lstrip(".") for d in allowed_domains]
    if not allowed:
        return True
    return any(host == d or host.endswith("." + d) for d in allowed)


def is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def check_url_syntax(url: str, policy: UrlPolicy) -> str:
    """Validate everything that can be checked without DNS. Returns the host."""
    if len(url) > 2048:
        raise UnsafeUrlError("URL is too long")
    parts = urlsplit(url.strip())
    schemes = {"https", "http"} if policy.allow_http else {"https"}
    if parts.scheme.lower() not in schemes:
        raise UnsafeUrlError(f"Only {' / '.join(sorted(schemes))} video URLs are supported")
    if parts.username or parts.password:
        raise UnsafeUrlError("URLs with embedded credentials are not allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeUrlError("URL has no host")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("Invalid port") from exc
    if port not in _ALLOWED_PORTS:
        raise UnsafeUrlError("Non-standard ports are not allowed")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise UnsafeUrlError("Internal hosts are not allowed")
    try:
        if not is_public_ip(host):
            raise UnsafeUrlError("Private or reserved IP addresses are not allowed")
    except ValueError:
        pass  # not an IP literal, it's a hostname
    if not _host_allowed(host, policy.allowed_domains):
        raise UnsafeUrlError("This video host is not in the allowed domain list")
    return host


async def resolve_public(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Could not resolve host {host}") from exc
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise UnsafeUrlError(f"Could not resolve host {host}")
    if not all(is_public_ip(ip) for ip in ips):
        raise UnsafeUrlError("Host resolves to a private or reserved address")
    return ips


class _PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """Resolves, validates, then connects to the validated IP."""

    def __init__(self) -> None:
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None, socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        ips = await resolve_public(host, port)
        last_exc: Exception | None = None
        for ip in ips:
            try:
                return await self._inner.connect_tcp(ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        raise UnsafeUrlError("Unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _SafeTransport(httpx.AsyncHTTPTransport):
    def __init__(self) -> None:
        super().__init__(retries=0)
        # httpx has no public hook for the network backend; replace the pool
        # with one using our validating backend (TLS/SNI still use the hostname).
        self._pool = httpcore.AsyncConnectionPool(ssl_context=httpx.create_ssl_context(), network_backend=_PublicOnlyBackend())


def safe_client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_SafeTransport(),
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(timeout, connect=10.0),
        headers={"User-Agent": "Visionary-VideoGateway/0.1"},
    )


@asynccontextmanager
async def safe_stream(url: str, policy: UrlPolicy, *, method: str = "GET", headers: dict[str, str] | None = None) -> AsyncIterator[httpx.Response]:
    """Open a streaming response, validating the URL and every redirect hop."""
    async with safe_client() as client:
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            check_url_syntax(current, policy)
            request = client.build_request(method, current, headers=headers)
            try:
                response = await client.send(request, stream=True)
            except UnsafeUrlError:
                raise
            except httpx.HTTPError as exc:
                raise ValidationFailed(f"Could not reach the video URL ({type(exc).__name__})", code="url_unreachable") from exc
            if response.is_redirect:
                location = response.headers.get("location")
                await response.aclose()
                if not location:
                    raise ValidationFailed("Redirect without a location", code="url_unreachable")
                current = urljoin(current, location)
                continue
            try:
                yield response
            finally:
                await response.aclose()
            return
        raise ValidationFailed("Too many redirects", code="url_unreachable")


def is_youtube(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in YOUTUBE_HOSTS

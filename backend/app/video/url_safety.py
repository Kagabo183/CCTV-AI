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
    # Private networks where the operator's own CCTV cameras/NVRs live (e.g.
    # "192.168.1.0/24"). Addresses here are reachable; all other private,
    # loopback and link-local addresses stay blocked. Empty = no LAN access.
    camera_networks: tuple[str, ...] = ()

    def in_camera_network(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        return any(addr in ipaddress.ip_network(net, strict=False) for net in self.camera_networks)

    def ip_allowed(self, ip: str) -> bool:
        return is_public_ip(ip) or self.in_camera_network(ip)


STREAM_SCHEMES = {"rtsp", "rtsps"}


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


def check_url_syntax(url: str, policy: UrlPolicy, *, streams: bool = False) -> str:
    """Validate everything that can be checked without DNS. Returns the host.

    streams=True also accepts rtsp:// links and embedded credentials for camera
    streams (the importer strips them before anything is stored).
    """
    if len(url) > 2048:
        raise UnsafeUrlError("URL is too long")
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    schemes = {"https", "http"} if policy.allow_http else {"https"}
    if streams:
        schemes |= STREAM_SCHEMES
    if scheme not in schemes:
        raise UnsafeUrlError(f"Only {' / '.join(sorted(schemes))} links are supported")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeUrlError("URL has no host")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("Invalid port") from exc
    if host == "localhost" or host.endswith((".localhost", ".internal")):
        raise UnsafeUrlError("Internal hosts are not allowed")
    camera_ip = policy.in_camera_network(host)
    try:
        if not policy.ip_allowed(host):
            private = ipaddress.ip_address(host).is_private
            hint = " Add the camera's network to CAMERA_PRIVATE_NETWORKS to allow it." if private else ""
            raise UnsafeUrlError("Private or reserved IP addresses are not allowed." + hint)
    except ValueError:
        pass  # not an IP literal, it's a hostname
    # Cameras use arbitrary ports (554, 8000, 8554...); public web hosts must use standard ones.
    if not camera_ip and scheme not in STREAM_SCHEMES and port not in _ALLOWED_PORTS:
        raise UnsafeUrlError("Non-standard ports are not allowed")
    if (parts.username or parts.password) and not (streams and (camera_ip or scheme in STREAM_SCHEMES)):
        raise UnsafeUrlError("URLs with embedded credentials are only allowed for camera streams")
    if not camera_ip and not _host_allowed(host, policy.allowed_domains):
        raise UnsafeUrlError("This video host is not in the allowed domain list")
    return host


def strip_credentials(url: str) -> str:
    """The URL with user:password removed, safe to store and display."""
    parts = urlsplit(url)
    if not (parts.username or parts.password):
        return url
    netloc = parts.hostname or ""
    if ":" in netloc:  # IPv6 literal
        netloc = f"[{netloc}]"
    if parts.port:
        netloc += f":{parts.port}"
    return parts._replace(netloc=netloc).geturl()


async def resolve_public(host: str, port: int, policy: UrlPolicy | None = None) -> list[str]:
    """Resolve and require every address to be public (or in the camera allowlist)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Could not resolve host {host}") from exc
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise UnsafeUrlError(f"Could not resolve host {host}")
    allowed = policy.ip_allowed if policy else is_public_ip
    if not all(allowed(ip) for ip in ips):
        raise UnsafeUrlError("Host resolves to a private or reserved address")
    return ips


class _PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """Resolves, validates, then connects to the validated IP."""

    def __init__(self, policy: UrlPolicy | None = None) -> None:
        self._inner = httpcore.AnyIOBackend()
        self._policy = policy

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None, socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        ips = await resolve_public(host, port, self._policy)
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
    def __init__(self, policy: UrlPolicy | None = None) -> None:
        super().__init__(retries=0)
        # httpx has no public hook for the network backend; replace the pool
        # with one using our validating backend (TLS/SNI still use the hostname).
        self._pool = httpcore.AsyncConnectionPool(ssl_context=httpx.create_ssl_context(), network_backend=_PublicOnlyBackend(policy))


def safe_client(timeout: float = 30.0, policy: UrlPolicy | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_SafeTransport(policy),
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(timeout, connect=10.0),
        headers={"User-Agent": "Visionary-VideoGateway/0.1"},
    )


@asynccontextmanager
async def safe_stream(url: str, policy: UrlPolicy, *, method: str = "GET", headers: dict[str, str] | None = None, streams: bool = False) -> AsyncIterator[httpx.Response]:
    """Open a streaming response, validating the URL and every redirect hop."""
    async with safe_client(policy=policy) as client:
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            check_url_syntax(current, policy, streams=streams)
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

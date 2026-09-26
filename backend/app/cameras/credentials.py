"""Camera credential vault and redaction.

Passwords are encrypted with Fernet (AES-128-CBC + HMAC-SHA256). The key comes from CAMERA_CREDENTIALS_KEY
(a Fernet key) or is derived from SECRET_KEY with HKDF. They are decrypted only inside the server process to
open a stream; they are never returned by the API, sent to the browser or written to logs.
"""

from __future__ import annotations

import base64
import json
import re
from functools import lru_cache
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import get_settings

_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<creds>[^/@\s]+)@", re.IGNORECASE)


@lru_cache
def _fernet() -> Fernet:
    s = get_settings()
    if s.camera_credentials_key:
        return Fernet(s.camera_credentials_key.get_secret_value().encode())
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"visionary-camera-credentials", info=b"fernet").derive(s.secret_key.get_secret_value().encode())
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt(secret: dict[str, Any]) -> str:
    return _fernet().encrypt(json.dumps(secret).encode()).decode()


def decrypt(token: str) -> dict[str, Any]:
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except InvalidToken as exc:  # key rotated or data tampered with
        raise ValueError("Stored camera credentials cannot be decrypted (was the key changed?)") from exc


def redact(text: str) -> str:
    """Remove user:password@ from every URL in a string (for logs and error messages)."""
    return _URL_CREDENTIALS.sub(lambda m: f"{m.group('scheme')}***@", text or "")


def with_credentials(url: str, username: str | None, password: str | None) -> str:
    """rtsp://host/path + user/pass -> rtsp://user:pass@host/path (server-side only)."""
    if not username:
        return url
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:  # IPv6
        host = f"[{host}]"
    netloc = f"{quote(username, safe='')}:{quote(password or '', safe='')}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def split_credentials(url: str) -> tuple[str, str | None, str | None]:
    """rtsp://user:pass@host/path -> (rtsp://host/path, user, pass)."""
    from urllib.parse import unquote

    parts = urlsplit(url)
    if not parts.username:
        return url, None, None
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)), unquote(parts.username), unquote(parts.password or "")

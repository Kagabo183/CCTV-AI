"""Logging setup with a redaction filter.

CCTV platforms handle sensitive data. Logs must never contain API keys or
signed URLs; the filter below scrubs the most common leak patterns.
"""

from __future__ import annotations

import logging
import re

_REDACTIONS = [
    (re.compile(r"(key=)[A-Za-z0-9_\-]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(AIza)[A-Za-z0-9_\-]{20,}"), r"\1[REDACTED]"),
    (re.compile(r"\b(AQ\.)[A-Za-z0-9_\-]{20,}"), r"\1[REDACTED]"),  # Google auth-key format
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"([?&](?:token|sig|signature|x-amz-signature|x-goog-signature)=)[^&\s]+", re.IGNORECASE), r"\1[REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # httpx logs full request URLs at INFO; keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)

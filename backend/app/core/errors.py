"""Domain errors translated to HTTP responses in app.main."""

from __future__ import annotations


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"


class ProviderUnavailable(AppError):
    """An external provider (Gemini, STT, TTS) is not configured or failed."""

    status_code = 503
    code = "provider_unavailable"


class NotSupportedYet(AppError):
    status_code = 501
    code = "not_supported_yet"

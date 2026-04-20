"""Bounded error-capture helper used by probe, scanner, and executor.

When an external call fails, counting the failure is not enough -- we need
the actual exception class, the HTTP status (if any), the response body
excerpt, and the request context. This helper stores the first N *unique*
errors per call-site so YAML output and structured logs can answer
"WHY did it fail?" without blowing up the log volume when hundreds of
failures stack up.

Usage:
    capture = ErrorCapture(max_unique=5)
    try:
        client.create_order(...)
    except Exception as exc:
        capture.record(exc, context={"ticker": ticker, "action": "buy"})
    ...
    print(capture.to_dict())

This is the reference pattern. Scanner and executor will import it too.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Any


def _extract_http_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status extraction across httpx / pykalshi / generic."""
    # httpx.HTTPStatusError exposes .response.status_code
    resp = getattr(exc, "response", None)
    if resp is not None:
        code = getattr(resp, "status_code", None)
        if isinstance(code, int):
            return code
    # pykalshi KalshiAPIError family stores .status_code directly
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    # Many wrappers embed the code in the message: "403: Host not in allowlist"
    msg = str(exc)
    if msg and msg[:3].isdigit():
        try:
            return int(msg[:3])
        except ValueError:
            return None
    return None


def _extract_body_excerpt(exc: BaseException, limit: int = 200) -> str:
    """Best-effort body extraction. Truncates hard so keys/tokens can't leak."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        body = getattr(resp, "text", None)
        if body:
            return str(body)[:limit]
    return str(exc)[:limit]


@dataclass
class ErrorSample:
    error_class: str
    http_status: int | None
    message: str
    body_excerpt: str
    context: dict[str, Any]
    count: int = 1

    def signature(self) -> tuple[str, int | None, str]:
        return (self.error_class, self.http_status, self.message[:80])

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_class": self.error_class,
            "http_status": self.http_status,
            "message": self.message,
            "body_excerpt": self.body_excerpt,
            "context": self.context,
            "count": self.count,
        }


@dataclass
class ErrorCapture:
    """Stores the first max_unique distinct errors seen by a call-site.

    Duplicate errors (same class + status + message prefix) increment the
    count of the existing entry instead of adding a new row. Total call
    count is tracked separately so aggregate failure rates can be reported.
    """

    max_unique: int = 5
    _samples: list[ErrorSample] = field(default_factory=list)
    _total_errors: int = 0
    _total_calls: int = 0
    _include_traceback: bool = False

    def record_success(self) -> None:
        self._total_calls += 1

    def record(self, exc: BaseException, *, context: dict[str, Any] | None = None) -> None:
        self._total_calls += 1
        self._total_errors += 1
        sample = ErrorSample(
            error_class=type(exc).__name__,
            http_status=_extract_http_status(exc),
            message=str(exc)[:300],
            body_excerpt=_extract_body_excerpt(exc),
            context=dict(context or {}),
        )
        # Dedup by signature
        for existing in self._samples:
            if existing.signature() == sample.signature():
                existing.count += 1
                return
        if len(self._samples) < self.max_unique:
            if self._include_traceback:
                sample.context["_traceback"] = "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                )[-500:]
            self._samples.append(sample)

    @property
    def total_errors(self) -> int:
        return self._total_errors

    @property
    def total_calls(self) -> int:
        return self._total_calls

    @property
    def error_rate(self) -> float:
        return self._total_errors / self._total_calls if self._total_calls else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_calls": self._total_calls,
            "total_errors": self._total_errors,
            "error_rate": round(self.error_rate, 3),
            "unique_errors_captured": len(self._samples),
            "samples": [s.to_dict() for s in self._samples],
        }

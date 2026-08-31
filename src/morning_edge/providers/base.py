"""Small, dependency-free primitives for authenticated market-data providers.

This module deliberately keeps provider tokens out of request results, error text,
and raw-response captures.  It is designed for REST polling, not streaming.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import random
import time
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


JSONValue = dict[str, Any] | list[Any] | str | int | float | bool | None
RawResponseHook = Callable[["RawResponse"], None]
DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class ProviderError(RuntimeError):
    """Base error for provider communication and payload validation."""


class ProviderAuthenticationError(ProviderError):
    """Authentication failed.  The token is intentionally not included."""


class ProviderRateLimitError(ProviderError):
    """The provider rejected the request due to a rate limit."""


class ProviderResponseError(ProviderError):
    """A provider returned an unexpected HTTP response."""


class ProviderSchemaError(ProviderError):
    """A provider returned JSON that does not match the minimum contract."""


class PaginationLimitError(ProviderError):
    """A paginated endpoint did not terminate before the configured guard."""


class _RejectRedirectHandler(HTTPRedirectHandler):
    """Reject redirects before a credential-bearing follow-up is created."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise HTTPError(req.full_url, code, "credentialed redirect rejected", headers, fp)


def open_without_redirects(request: Request, *, timeout_seconds: float):
    """Open one request without following any redirect response."""

    return build_opener(_RejectRedirectHandler()).open(request, timeout=timeout_seconds)


def read_bounded_body(stream: Any, headers: Mapping[str, str], *, maximum_bytes: int) -> bytes:
    """Read at most ``maximum_bytes`` and reject declared or observed excess."""

    lower_headers = {str(key).lower(): str(value) for key, value in headers.items()}
    declared = lower_headers.get("content-length")
    if declared is not None:
        try:
            declared_bytes = int(declared)
        except ValueError:
            declared_bytes = -1
        if declared_bytes > maximum_bytes:
            raise ProviderResponseError("provider response exceeded the configured byte limit")
    body = stream.read(maximum_bytes + 1)
    if len(body) > maximum_bytes:
        raise ProviderResponseError("provider response exceeded the configured byte limit")
    return body


@dataclass(frozen=True)
class RateLimitMetadata:
    """Best-effort standard rate-limit metadata, when the server exposes it."""

    limit: int | None = None
    remaining: int | None = None
    reset_at: str | None = None
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class RawResponse:
    """Captured response material.  Request headers are never retained."""

    provider: str
    method: str
    url: str
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    fetched_at: datetime
    attempts: int
    rate_limit: RateLimitMetadata


@dataclass(frozen=True)
class JsonResponse:
    """A successful JSON response with observable metadata."""

    payload: JSONValue
    raw: RawResponse

    @property
    def data(self) -> JSONValue:
        return response_data(self.payload)


class Transport(Protocol):
    """Injectable GET transport used to keep tests offline and deterministic."""

    def __call__(
        self, url: str, headers: Mapping[str, str], timeout_seconds: float
    ) -> tuple[int, Mapping[str, str], bytes]: ...


class AttemptBudget(Protocol):
    """Local preflight counter invoked once for every outbound HTTP attempt."""

    def reserve_attempt(self) -> object: ...


def _int_header(headers: Mapping[str, str], *names: str) -> int | None:
    for name in names:
        value = headers.get(name)
        if value is not None:
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _float_header(headers: Mapping[str, str], *names: str) -> float | None:
    for name in names:
        value = headers.get(name)
        if value is not None:
            try:
                return max(0.0, float(value))
            except ValueError:
                return None
    return None


def rate_limit_metadata(headers: Mapping[str, str]) -> RateLimitMetadata:
    """Read common rate-limit headers without assuming a provider-specific format."""

    lower = {key.lower(): value for key, value in headers.items()}
    return RateLimitMetadata(
        limit=_int_header(lower, "x-ratelimit-limit", "ratelimit-limit"),
        remaining=_int_header(lower, "x-ratelimit-remaining", "ratelimit-remaining"),
        reset_at=lower.get("x-ratelimit-reset") or lower.get("ratelimit-reset"),
        retry_after_seconds=_float_header(lower, "retry-after"),
    )


def safe_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Keep only a small, auditable set of safe response headers.

    A blacklist is unsafe here: proxy middleware can attach unknown credential or
    session headers. Persist only response metadata needed to diagnose freshness,
    caching, tracing, and rate limits.
    """

    exact = {"date", "content-type", "etag", "x-request-id", "request-id", "retry-after"}
    rate_limit_prefixes = ("x-ratelimit-", "ratelimit-", "x-rate-limit-", "rate-limit-")
    return {
        key: value
        for key, value in headers.items()
        if key.lower() in exact or key.lower().startswith(rate_limit_prefixes)
    }


def _validated_base_url(
    base_url: str,
    *,
    allowed_hosts: frozenset[str] | None,
    require_standard_https_port: bool,
) -> str:
    """Canonicalize an unambiguous HTTPS origin before credentials are attached."""

    parsed = urlsplit(base_url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("base_url must be an unambiguous HTTPS origin")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("base_url contains an invalid port") from error
    hostname = parsed.hostname.lower()
    if allowed_hosts is not None and hostname not in allowed_hosts:
        raise ValueError("base_url host is not authorized for credentialed transport")
    if require_standard_https_port and port not in {None, 443}:
        raise ValueError("credentialed transport requires the standard HTTPS port")
    return f"https://{hostname}" + (f":{port}" if port not in {None, 443} else "")


def response_data(payload: JSONValue) -> JSONValue:
    """Validate the documented ``{\"data\": ...}`` response envelope."""

    if not isinstance(payload, dict) or "data" not in payload:
        raise ProviderSchemaError("expected a JSON object containing a 'data' field")
    return payload["data"]


def require_list(payload: JSONValue, *, endpoint: str) -> list[dict[str, Any]]:
    """Return an envelope's list data and minimally validate row objects."""

    data = response_data(payload)
    if not isinstance(data, list):
        raise ProviderSchemaError(f"{endpoint}: expected data to be a list")
    if not all(isinstance(row, dict) for row in data):
        raise ProviderSchemaError(f"{endpoint}: expected every data item to be an object")
    return data


def require_mapping(payload: JSONValue, *, endpoint: str) -> dict[str, Any]:
    """Return an envelope's object data and minimally validate it."""

    data = response_data(payload)
    if not isinstance(data, dict):
        raise ProviderSchemaError(f"{endpoint}: expected data to be an object")
    return data


class JsonlRawResponseCapture:
    """Append raw response evidence as JSON Lines without recording credentials.

    Retention is an application responsibility. Raw vendor payloads can contain
    licensed data, so callers should choose a private directory with a retention
    policy before enabling this hook.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def __call__(self, response: RawResponse) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "provider": response.provider,
            "method": response.method,
            "url": response.url,
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body_utf8": response.body.decode("utf-8", errors="replace"),
            "fetched_at": response.fetched_at.isoformat(),
            "attempts": response.attempts,
            "rate_limit": {
                "limit": response.rate_limit.limit,
                "remaining": response.rate_limit.remaining,
                "reset_at": response.rate_limit.reset_at,
                "retry_after_seconds": response.rate_limit.retry_after_seconds,
            },
        }
        descriptor = os.open(self._path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        except BaseException:
            os.close(descriptor)
            raise
        with os.fdopen(descriptor, "a", encoding="utf-8") as output:
            output.write(json.dumps(record, separators=(",", ":")) + "\n")


class SafeGetClient:
    """GET-only JSON client with bounded retries and a testable transport seam."""

    RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        *,
        provider: str,
        authorization: str,
        base_url: str,
        timeout_seconds: float = 15.0,
        max_attempts: int = 3,
        base_backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 8.0,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: Transport | None = None,
        allowed_hosts: frozenset[str] | None = None,
        raw_response_hook: RawResponseHook | None = None,
        attempt_budget: AttemptBudget | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_float: Callable[[], float] = random.random,
    ) -> None:
        if not authorization.strip():
            raise ValueError("authorization must not be empty")
        if timeout_seconds <= 0 or max_attempts < 1:
            raise ValueError("timeout_seconds must be positive and max_attempts at least one")
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        if transport is None and allowed_hosts is None:
            raise ValueError("credentialed default transport requires an allowlisted host")
        self.provider = provider
        self._authorization = authorization
        self.base_url = _validated_base_url(
            base_url,
            allowed_hosts=allowed_hosts if transport is None else None,
            require_standard_https_port=transport is None,
        )
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.max_response_bytes = max_response_bytes
        self._transport = transport or self._urlopen_transport
        self._raw_response_hook = raw_response_hook
        self._attempt_budget = attempt_budget
        self._sleep = sleep
        self._random_float = random_float

    def _urlopen_transport(
        self, url: str, headers: Mapping[str, str], timeout_seconds: float
    ) -> tuple[int, Mapping[str, str], bytes]:
        request = Request(url, method="GET", headers=dict(headers))
        try:
            with open_without_redirects(request, timeout_seconds=timeout_seconds) as result:
                response_headers = dict(result.headers.items())
                return (
                    result.status,
                    response_headers,
                    read_bounded_body(
                        result,
                        response_headers,
                        maximum_bytes=self.max_response_bytes,
                    ),
                )
        except HTTPError as error:
            response_headers = dict(error.headers.items()) if error.headers else {}
            return (
                error.code,
                response_headers,
                read_bounded_body(
                    error,
                    response_headers,
                    maximum_bytes=self.max_response_bytes,
                ),
            )
        except URLError:
            # Do not retain a lower-level exception as a cause: custom proxy
            # errors can include sensitive request material in their text.
            raise ProviderResponseError("network error while requesting provider") from None

    def get_json(self, path: str, *, params: Mapping[str, Any] | None = None) -> JsonResponse:
        """Fetch JSON. Retries only idempotent GETs and never logs authorization."""

        parsed_path = urlsplit(path)
        if (
            not path.startswith("/")
            or path.startswith("//")
            or parsed_path.scheme
            or parsed_path.netloc
            or parsed_path.query
            or parsed_path.fragment
        ):
            raise ValueError("path must be an absolute URL path without query or fragment")
        encoded_params = urlencode(
            [(key, value) for key, value in (params or {}).items() if value is not None], doseq=True
        )
        url = self.base_url + path + (("?" + encoded_params) if encoded_params else "")
        headers = {
            "Accept": "application/json",
            "Authorization": self._authorization,
            "User-Agent": "codex-screener/0.1 (+local research use)",
        }

        for attempt in range(1, self.max_attempts + 1):
            # Charge before touching the transport. Failed connections and
            # retry attempts still consume a provider request opportunity.
            if self._attempt_budget is not None:
                self._attempt_budget.reserve_attempt()
            try:
                status, response_headers, body = self._transport(url, headers, self.timeout_seconds)
            except ProviderError:
                raise
            except Exception as error:  # transport adapters may raise timeout-like errors
                if attempt == self.max_attempts:
                    # An injected transport can accidentally include a token in
                    # its exception. Do not retain it as a chained cause.
                    raise ProviderResponseError("provider GET failed after retry budget") from None
                self._sleep(self._backoff(attempt, None))
                continue

            if len(body) > self.max_response_bytes:
                raise ProviderResponseError("provider response exceeded the configured byte limit")

            metadata = rate_limit_metadata(response_headers)
            raw = RawResponse(
                provider=self.provider,
                method="GET",
                url=url,
                status_code=status,
                headers=safe_response_headers(response_headers),
                body=body,
                fetched_at=datetime.now(UTC),
                attempts=attempt,
                rate_limit=metadata,
            )
            self._capture(raw)

            if status == 401 or status == 403:
                raise ProviderAuthenticationError("provider rejected the supplied credentials")
            if status in self.RETRYABLE_STATUSES and attempt < self.max_attempts:
                self._sleep(self._backoff(attempt, metadata.retry_after_seconds))
                continue
            if status == 429:
                raise ProviderRateLimitError("provider rate limit exhausted")
            if not 200 <= status < 300:
                raise ProviderResponseError(f"provider returned HTTP {status}")

            try:
                payload: JSONValue = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ProviderSchemaError("provider returned invalid JSON") from error
            return JsonResponse(payload=payload, raw=raw)

        raise AssertionError("retry loop must return or raise")

    def iter_pages(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        page_parameter: str = "page",
        start_page: int = 0,
        max_pages: int = 20,
        rows: Callable[[JSONValue], Sequence[Any]] | None = None,
    ) -> Iterator[JsonResponse]:
        """Page until the data list is short, empty, or the bounded guard trips."""

        if max_pages < 1:
            raise ValueError("max_pages must be at least one")
        extractor = rows or (lambda payload: require_list(payload, endpoint=path))
        shared = dict(params or {})
        for offset in range(max_pages):
            page = start_page + offset
            response = self.get_json(path, params={**shared, page_parameter: page})
            page_rows = extractor(response.payload)
            yield response
            requested_limit = shared.get("limit")
            if not page_rows or (isinstance(requested_limit, int) and len(page_rows) < requested_limit):
                return
        raise PaginationLimitError(f"{path}: exceeded max_pages={max_pages}")

    def _capture(self, raw: RawResponse) -> None:
        if self._raw_response_hook is not None:
            self._raw_response_hook(raw)

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(self.max_backoff_seconds, retry_after)
        exponential = min(self.max_backoff_seconds, self.base_backoff_seconds * (2 ** (attempt - 1)))
        return exponential * (0.75 + 0.5 * self._random_float())

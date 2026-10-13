"""Read-only HTTP transport and TLS pinning.

``ReadOnlyClient`` is the only object in the package that talks HTTP. Every request
goes through ``_send``, which refuses anything but GET/HEAD; the single exception is
``login_legacy`` (POST /api/auth/login), which passes a module-private capability
object that no other code can obtain through the public API.
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import ssl
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from unifi_carto import __version__
from unifi_carto.config import TlsConfig, ca_context
from unifi_carto.errors import (
    LEGACY_AUTH_MESSAGES,
    ApiError,
    AuthRefused,
    ConfigError,
    EndpointUnavailable,
    ReadOnlyViolation,
    TlsPinMismatch,
)
from unifi_carto.transport import Params

log = logging.getLogger(__name__)

_READ_METHODS: Final = frozenset({"GET", "HEAD"})
_LOGIN_PATH: Final = "/api/auth/login"
_BACKOFF: Final = (0.5, 1.0, 2.0)
_RETRY_AFTER_CAP: Final = 60.0
_RETRY_STATUSES: Final = frozenset({502, 503, 504})
_MFA_MARKERS: Final = ("ubic2fatokenrequired", "mfa", "2fa")
_MAX_PAGES: Final = 1000
# httpcore logs raw response headers (Set-Cookie, X-CSRF-Token) at DEBUG and httpx full URLs at INFO.
THIRD_PARTY_LOGGERS: Final = ("httpx", "httpcore", "hpack", "urllib3")


def pin_third_party_loggers() -> None:
    """Keep the HTTP stack's loggers at WARNING so debug logging never carries headers or cookies."""
    for name in THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


pin_third_party_loggers()


class _LoginCapability:
    """Unforgeable token: only ``login_legacy`` holds the instance."""

    __slots__ = ()


_LOGIN_CAP: Final = _LoginCapability()


class ReadOnlyClient:
    """A GET-only JSON client bound to one base URL and one credential (implements ``Transport``)."""

    __slots__ = ("__client", "__csrf", "__sleep", "_auth", "base_url", "max_retries")

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None,
        api_key_header: str = "X-API-KEY",
        tls: TlsConfig | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        user_agent: str = f"unifi-cartographer/{__version__}",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.__sleep = sleep
        self.__csrf: str | None = None
        parts = urlsplit(self.base_url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("base_url must be an https:// URL")
        verify = build_ssl_context(tls, parts.hostname, parts.port or 443)
        headers = {"User-Agent": user_agent, "Accept": "application/json"}
        if api_key:
            headers[api_key_header] = api_key
        self._auth = "api_key" if api_key else "none"
        self.__client = httpx.Client(
            headers=headers, verify=verify, timeout=timeout, follow_redirects=False, trust_env=False
        )

    def __repr__(self) -> str:
        return f"ReadOnlyClient(base_url={self.base_url!r}, auth={self._auth!r})"

    def close(self) -> None:
        self.__client.close()

    def __enter__(self) -> ReadOnlyClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- Transport -------------------------------------------------------------------------

    def get_json(self, path: str, params: Params | None = None) -> Any:
        return self._send("GET", self.base_url + path, params=params)

    def get_paginated(self, path: str, params: Params | None = None, *, limit: int = 200) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        offset = 0
        seen = 0
        dropped = 0
        prev_digest: str | None = None
        for _ in range(_MAX_PAGES):
            page = self.get_json(path, {**(params or {}), "offset": offset, "limit": limit})
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise ApiError(f"GET {path}: expected a paginated object with 'data'", status=200, url=path)
            data: list[Any] = page["data"]
            if not data:
                break
            digest = _page_digest(data)
            if digest == prev_digest:
                raise ApiError(
                    f"GET {path}: server ignored offset={offset} (page repeats the previous one)", status=200, url=path
                )
            prev_digest = digest
            objects = [item for item in data if isinstance(item, dict)]
            dropped += len(data) - len(objects)
            items.extend(objects)
            seen += len(data)
            offset += len(data)
            total = page.get("totalCount")
            if isinstance(total, int):
                if seen >= total:
                    break
            elif len(data) < limit:
                break
        else:
            raise ApiError(f"GET {path}: more than {_MAX_PAGES} pages", status=200, url=path)
        if dropped:
            log.debug("GET %s: dropped %d non-object item(s)", path, dropped)
        return items

    def login_legacy(self, username: str, password: str) -> None:
        parts = urlsplit(self.base_url)
        url = f"{parts.scheme}://{parts.netloc}{_LOGIN_PATH}"
        body = {"username": username, "password": password, "rememberMe": False}
        resp = self._send("POST", url, json_body=body, capability=_LOGIN_CAP)
        assert isinstance(resp, httpx.Response)
        status = resp.status_code
        text = resp.text.lower()
        if "ubic2fatokenrequired" in text or (status != 200 and any(m in text for m in _MFA_MARKERS)):
            raise AuthRefused(
                "legacy login requires 2FA; use a local View-Only account without 2FA", status=status, url=_LOGIN_PATH
            )
        if status in (401, 403):
            raise AuthRefused(
                "legacy login refused; use a local View-Only account without 2FA", status=status, url=_LOGIN_PATH
            )
        if status == 404:
            raise EndpointUnavailable(f"{_LOGIN_PATH} not found: not a UniFi OS console?", status=404, url=_LOGIN_PATH)
        if status != 200:
            raise ApiError(f"legacy login failed with HTTP {status}", status=status, url=_LOGIN_PATH)
        # Only needed for writes, which this tool never does; kept for completeness, never logged.
        self.__csrf = resp.headers.get("X-Updated-CSRF-Token") or resp.headers.get("X-CSRF-Token")
        for name in [h for h in self.__client.headers if h.lower() in ("x-api-key",)]:
            del self.__client.headers[name]
        self._auth = "session"
        log.debug("legacy login ok")

    # -- internals -------------------------------------------------------------------------

    def _send(
        self,
        method: str,
        url: str,
        *,
        params: Params | None = None,
        json_body: Mapping[str, object] | None = None,
        capability: object = None,
    ) -> Any:
        method = method.upper()
        parts = urlsplit(url)
        own = urlsplit(self.base_url)
        is_login = (
            capability is _LOGIN_CAP
            and method == "POST"
            and (parts.scheme, parts.netloc) == (own.scheme, own.netloc)
            and parts.path == _LOGIN_PATH
        )
        if method not in _READ_METHODS and not is_login:
            raise ReadOnlyViolation(f"refusing {method} request: this client is read-only")
        if json_body is not None and not is_login:
            raise ReadOnlyViolation("refusing request body: this client is read-only")
        path = parts.path
        # The login is sent exactly once: never resend credentials after a 429/5xx or an ambiguous transport failure.
        retries = 0 if is_login else self.max_retries
        attempt = 0
        while True:
            try:
                resp = self.__client.request(method, url, params=dict(params) if params else None, json=json_body)
            except httpx.TransportError as exc:
                tls = _is_tls_error(exc)
                log.debug("%s %s -> %s", method, path, "tls error" if tls else type(exc).__name__)
                if not tls and attempt < min(retries, len(_BACKOFF)):
                    self.__sleep(_BACKOFF[attempt])
                    attempt += 1
                    continue
                what = "TLS verification failed" if tls else f"transport error ({type(exc).__name__})"
                raise ApiError(f"{method} {path}: {what}", status=0, url=path) from None
            log.debug("%s %s -> %d", method, path, resp.status_code)
            status = resp.status_code
            if status == 429 and attempt < retries:
                self.__sleep(_retry_after(resp.headers.get("Retry-After"), attempt))
                attempt += 1
                continue
            if status in _RETRY_STATUSES and attempt < min(retries, len(_BACKOFF)):
                self.__sleep(_BACKOFF[attempt])
                attempt += 1
                continue
            if is_login:
                if 300 <= status < 400:
                    raise ApiError(
                        f"{method} {path}: HTTP {status} redirect not followed (not a UniFi OS console?)",
                        status=status,
                        url=path,
                    )
                return resp
            return _decode(method, path, resp)


def _decode(method: str, path: str, resp: httpx.Response) -> Any:
    status = resp.status_code
    where = f"{method} {path}"
    if 300 <= status < 400:
        raise ApiError(
            f"{where}: HTTP {status} redirect not followed (on legacy paths: probably not a UniFi OS console)",
            status=status,
            url=path,
        )
    body: Any = None
    if resp.content and method != "HEAD":
        try:
            body = resp.json()
        except ValueError:
            if status < 400:
                raise ApiError(f"{where}: response is not JSON", status=status, url=path) from None
    detail = _error_detail(body)
    suffix = f" ({detail})" if detail else ""
    if status in (401, 403):
        raise AuthRefused(f"{where}: HTTP {status} access refused{suffix}", status=status, url=path)
    if status == 404:
        raise EndpointUnavailable(f"{where}: HTTP 404 not found{suffix}", status=status, url=path)
    if status >= 400:
        raise ApiError(f"{where}: HTTP {status}{suffix}", status=status, url=path)
    meta = body.get("meta") if isinstance(body, dict) else None
    if isinstance(meta, dict) and "rc" in meta and meta["rc"] != "ok":
        msg = str(meta.get("msg") or meta["rc"])
        cls = AuthRefused if msg in LEGACY_AUTH_MESSAGES else ApiError
        raise cls(f"{where}: legacy API error {msg}", status=status, url=path)
    return body


def _page_digest(data: list[Any]) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def _error_detail(body: Any) -> str | None:
    """Short, non-secret error description from a JSON error body (legacy ``meta.msg`` or official ``message``)."""
    if not isinstance(body, dict):
        return None
    meta = body.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("msg"), str):
        return str(meta["msg"])[:200]
    for key in ("code", "message"):
        if isinstance(body.get(key), str):
            return str(body[key])[:200]
    return None


def _retry_after(value: str | None, attempt: int) -> float:
    delay: float | None = None
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
            except (TypeError, ValueError):
                when = None
            if when is not None:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                delay = (when - datetime.now(UTC)).total_seconds()
    if delay is None:
        delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
    return max(0.0, min(delay, _RETRY_AFTER_CAP))


def _is_tls_error(exc: BaseException) -> bool:
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, ssl.SSLError):
            return True
        seen = seen.__cause__ or seen.__context__
    return "CERTIFICATE_VERIFY_FAILED" in str(exc)


# -- TLS ------------------------------------------------------------------------------------


def normalize_fingerprint(value: str) -> str:
    """``sha256:AB:CD:..`` / ``abcd..`` -> lowercase hex without separators."""
    v = value.strip().lower()
    if v.startswith("sha256:"):
        v = v[len("sha256:") :]
    v = v.replace(":", "").replace("-", "").replace(" ", "")
    if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
        raise ValueError("TLS fingerprint must be a SHA-256 digest (64 hex digits)")
    return v


def format_fingerprint(der: bytes) -> str:
    digest = hashlib.sha256(der).hexdigest().upper()
    return "sha256:" + ":".join(digest[i : i + 2] for i in range(0, 64, 2))


def _fetch_peer_der(host: str, port: int, timeout: float) -> bytes:
    """Fetch the peer's leaf certificate. Nothing is sent over this connection besides the handshake."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # fetch only; the real connection verifies against the pinned cert
    with (
        socket.create_connection((host, port), timeout=timeout) as sock,
        ctx.wrap_socket(sock, server_hostname=host) as tls_sock,
    ):
        der = tls_sock.getpeercert(binary_form=True)
    if not der:
        raise TlsPinMismatch(f"{host}:{port} presented no certificate")
    return der


def _relax_flags(ctx: ssl.SSLContext) -> None:
    # Python 3.13 enables VERIFY_X509_STRICT by default; UniFi self-signed certs often lack the extensions it wants.
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT


def build_ssl_context(tls: TlsConfig | None, host: str, port: int, *, timeout: float = 10.0) -> ssl.SSLContext | bool:
    """System trust (``True``), a CA file, or a SHA-256 pin of the console's leaf certificate."""
    if tls is None or (not tls.fingerprint and not tls.ca_cert):
        return True
    if tls.ca_cert:
        ctx = ca_context(tls.ca_cert)
        _relax_flags(ctx)
        return ctx
    assert tls.fingerprint is not None
    try:
        expected = normalize_fingerprint(tls.fingerprint)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    try:
        der = _fetch_peer_der(host, port, timeout)
    except OSError as exc:
        raise ApiError(f"cannot fetch TLS certificate from {host}:{port}: {exc}", status=0, url="/") from None
    actual = hashlib.sha256(der).hexdigest()
    if actual != expected:
        raise TlsPinMismatch(
            f"{host}:{port} certificate fingerprint {format_fingerprint(der)} does not match the configured pin"
        )
    ctx = ssl.create_default_context(cadata=ssl.DER_cert_to_PEM_cert(der))
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    _relax_flags(ctx)
    return ctx


def fingerprint(url: str, timeout: float = 10.0) -> str:
    """Return ``sha256:AA:BB:...`` of the TLS leaf certificate served at ``url`` (for ``unifi-carto pin``)."""
    parts = urlsplit(url if "://" in url else f"https://{url}")
    if not parts.hostname:
        raise ValueError(f"no host in URL {url!r}")
    return format_fingerprint(_fetch_peer_der(parts.hostname, parts.port or 443, timeout))

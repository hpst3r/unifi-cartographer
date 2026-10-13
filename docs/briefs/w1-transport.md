# Brief W1 - transport: http.py, sitemanager.py, integration.py, scrub.py

Read `docs/WORKER.md`, `docs/SPEC.md`, `docs/MODEL.md`, `src/unifi_carto/transport.py`, `raw.py`, `errors.py`, `config.py` first.

API references (read-only web access is allowed via WebFetch): the mirrored official OpenAPI specs at `https://raw.githubusercontent.com/opastorello/unifi-api-docs/main/network/v10.6.106/openapi.json` (and `reference.md`), and `.../site-manager/v1.0.0/reference.md` (+ `openapi.json`); the catalog is `.../main/catalog.json` (check whether an 11.x network spec has appeared and note differences relevant to devices/uplinks/LAGs). Use field names exactly as the spec defines them. Do not copy the spec files into the repo.

## Files you own

`src/unifi_carto/http.py`, `src/unifi_carto/sitemanager.py`, `src/unifi_carto/integration.py`, `src/unifi_carto/scrub.py`, tests `tests/test_http.py`, `tests/test_sitemanager.py`, `tests/test_integration.py`, `tests/test_scrub.py`.

## http.py

- `class ReadOnlyClient` implementing `transport.Transport`:
  - `__init__(self, base_url: str, *, api_key: str | None, api_key_header: str = "X-API-KEY", tls: TlsConfig | None = None, timeout: float = 30.0, max_retries: int = 3, user_agent: str = f"unifi-cartographer/{__version__}")`. Wraps one `httpx.Client` (`follow_redirects=False`).
  - `get_json`, `get_paginated` (Integration pagination: `offset`, `limit`; response `{offset, limit, count, totalCount, data}`; stop when collected >= totalCount or a page is empty; guard against a server that ignores offset: if a page repeats the previous page's first id, stop and raise ApiError), `login_legacy`.
  - **Read-only enforcement:** all requests go through one private `_send(method, ...)` that raises `ReadOnlyViolation` for any method not in {GET, HEAD} unless called from `login_legacy`. Make it structurally hard to bypass (e.g. no public access to the underlying httpx client; `__getattr__` not forwarding).
  - Error mapping: 401/403 -> `AuthRefused`; 404 -> `EndpointUnavailable`; 3xx -> `ApiError` (message says redirects are not followed; for legacy this often means "not a UniFi OS console"); 429 -> sleep `Retry-After` (seconds or HTTP-date, capped at 60 s) and retry up to `max_retries`; 502/503/504 and transport errors -> exponential backoff (0.5, 1, 2 s) then `ApiError(status=0 or code)`; other >= 400 -> `ApiError`. Inject the sleep function so tests don't sleep.
  - Legacy envelope: if a decoded body is a dict with `meta.rc` and `rc != "ok"`, raise `ApiError` with `meta.msg` (e.g. `api.err.NoPermission` -> `AuthRefused`).
  - `login_legacy(username, password)`: `POST {base}/api/auth/login` JSON `{"username":..., "password":..., "rememberMe": false}`; on 200 keep the `TOKEN` cookie (httpx cookie jar) and store the `X-CSRF-Token` / `X-Updated-CSRF-Token` header value (only needed for writes, which we never do; keep it but never log it); remove the API key header for subsequent calls. 401/403 or a body mentioning `Ubic2faTokenRequired` / MFA -> `AuthRefused` with a message telling the user to use a local View-Only account without 2FA. Credentials must not appear in exceptions, logs or `repr()`.
  - Logging: module logger; debug logs show method, path and status only; headers are never logged. Add a test that sets the logger to DEBUG, runs requests with a key and a login, and asserts the key/password/cookie values appear nowhere in captured logs or exception strings.
- TLS:
  - `def build_ssl_context(tls: TlsConfig | None, host: str, port: int) -> ssl.SSLContext | bool` - system trust (`True`), CA file, or fingerprint pin. Pin: fetch the peer certificate (DER) with a throwaway non-verifying socket, compare `sha256` against the configured fingerprint (normalize case/colons/`sha256:` prefix) -> `TlsPinMismatch` on mismatch; then return a context that trusts exactly that certificate (`cadata=` PEM), `check_hostname=False`, `verify_mode=CERT_REQUIRED`, with `VERIFY_X509_PARTIAL_CHAIN` set and `VERIFY_X509_STRICT` cleared (Python 3.13 enables strict by default and UniFi self-signed certs may lack extensions). The real connection then verifies against the pinned cert, so a cert swapped between the fetch and the connect fails.
  - `def fingerprint(url: str, timeout: float = 10.0) -> str` returning `sha256:AA:BB:...` (used by `unifi-carto pin`).
  - Tests: use `trustme` to run a local HTTPS server on 127.0.0.1 (e.g. `http.server` in a thread with the trustme cert) and prove: correct pin -> request succeeds; wrong pin -> `TlsPinMismatch`; CA file -> succeeds; system trust against the self-signed server -> fails with a TLS error (mapped to `ApiError(status=0)`). Mark nothing as network; it is loopback only.
- A repo test `tests/test_http.py::test_no_write_calls_outside_http` that scans `src/unifi_carto/**/*.py` for `.post(`, `.put(`, `.patch(`, `.delete(` and `method="POST"`-style strings and fails if any occur outside `http.py`.

## sitemanager.py

- `class SiteManager` taking a `Transport` (base `https://api.ui.com`, header `X-API-Key`): `hosts() -> list[dict]`, `sites() -> list[dict]`, `devices() -> list[dict]` (check the spec's pagination: `pageSize`/`nextToken` - implement what the spec says, not Integration-style offset paging; if Site Manager pagination doesn't fit `Transport.get_paginated`, implement it here with `get_json`).
- `def connector_base_url(host_id: str) -> str` -> `https://api.ui.com/v1/connector/consoles/{host_id}/network/integration`.
- `def host_summary(host: dict) -> ConsoleRef` mapping id, name (from `reportedState.name` / `hostname` - check the spec), model, firmware into `raw.ConsoleRef(access="connector")`.

## integration.py

- `class Integration` taking a `Transport` whose base URL ends in `/proxy/network/integration` (local) or the connector base. Paths below are appended as `/v1/...`.
- `info() -> dict`, `sites() -> list[dict]`.
- `collect_site(console: ConsoleRef, site: dict, *, include_clients: bool, max_workers: int = 4) -> OfficialSnapshot`: list devices; fetch each device detail (bounded thread pool, preserve deterministic ordering); networks list + each network detail; wifi broadcasts list (+ detail if the list items lack `network`/security fields per the spec); wans; switching lags, mc-lag-domains, switch-stacks; clients only if requested. Each endpoint that raises `EndpointUnavailable` adds `CollectionWarning(code="endpoint_unavailable", ...)` and continues; `AuthRefused` on the first call aborts the site with the exception (collect.py decides); other ApiErrors on optional endpoints become `CollectionWarning(code="endpoint_error")`.
- Return raw JSON untouched (no field renaming).
- Tests with respx: a full synthetic site (gateway, aggregation switch, 2 access switches, 2 APs) built from spec field names; pagination across 3 pages; a 404 on `/switching/mc-lag-domains` becomes a warning; device detail fetch errors are reported; clients omitted when not requested.

## scrub.py

- `def scrub(obj: Any, *, salt: str) -> Any` - deterministic, pure, for turning real captures into public test fixtures:
  - Drop keys whose name contains (case-insensitive) `pass`, `secret`, `psk`, `key` (but not `keyword`-like false positives you can justify), `token`, `x_`-prefixed legacy secret fields (`x_passphrase`, `x_iapp_key`, ...), `serial`, `license`.
  - MACs (any string matching a MAC pattern, in any key) -> `02:` + 5 bytes of HMAC-SHA256(salt, mac), same input -> same output everywhere in the capture (so joins still work).
  - IPv4: private addresses -> `10.<a>.<b>.<host>` preserving the last octet and mapping each distinct /24 to a distinct deterministic /24; public addresses -> 203.0.113.x / 198.51.100.x deterministic. IPv6 -> `fd00::/8` deterministic.
  - Free-text identity fields (`name`, `hostname`, `ssid`, `essid`, `site_name`/`desc`, `label`, `x_ssh_*` dropped) -> `<kind>-<6 hex>` deterministic (e.g. `device-3fa2c1`), but keep `model`, `type`, `version`, `media`, enum-like values.
  - UUIDs -> deterministic UUIDv5-style remap (same input -> same output).
- Tests: idempotence of mapping, join preservation (a MAC appearing in two places maps identically), secrets removed at any depth, model strings kept.

## Gate

ruff format/check, mypy strict, pytest all green; commit on your branch. Report the public API signatures in `public_api`.

# unifi-cartographer: specification

Status: v0.1 contract (frozen for the first worker wave; changes go in "Amendments" at the end).

## Purpose

A **read-only** collector that pulls configuration and topology from UniFi Network consoles and emits one documented, versioned JSON **inventory model** (devices, ports, port-to-port links, VLANs, SSIDs, LAG/MC-LAG/stack groups, WANs, optional clients) with per-fact provenance, plus a separate renderer that turns the model into editable draw.io diagrams. Built on the official UniFi Network API (Integration API) and the Site Manager API, with an automatic, read-only legacy supplement for the port-level facts the official API does not expose.

Non-goals: changing any controller state; metrics/monitoring; UniFi Protect/Access.

## Hard rules

1. **GET only.** The single HTTP wrapper (`unifi_carto.http.ReadOnlyClient`) refuses any method other than GET/HEAD at runtime, with one exception: the legacy local-login `POST /api/auth/login` (cookie fallback), implemented as a dedicated, separately tested method `login_legacy()` that is the only code path allowed to send a body. No PUT/PATCH/DELETE anywhere. A test greps the source tree for `.post(`, `.put(`, `.patch(`, `.delete(` and fails on any hit outside `http.py`.
2. **TLS verified.** Never `verify=False`. Per-console options: system trust (default), `ca_cert` path, or `tls_fingerprint` (SHA-256 pin of the console's self-signed leaf: the client fetches the peer cert, compares the fingerprint, then trusts exactly that cert with hostname checking off). `unifi-carto pin <url>` prints the fingerprint for TOFU setup. api.ui.com always uses system trust.
3. **Secrets** come only from environment variables or a secrets file referenced by the config (`*_env` / `*_file` keys; files must be mode 0600 or tighter, else refuse). Secrets never appear in logs, exceptions, `--debug` output, captured fixtures or the model.
4. **Clients are opt-in** (`--clients`): client MACs, names and IPs are personal data and are excluded by default.
5. Python >= 3.11. Runtime deps: `httpx`, `pydantic>=2`. Nothing else at runtime without a SPEC amendment. Dev: pytest, ruff, mypy (strict on `src/`), respx (HTTP mocking), trustme (test TLS certificates).
6. No network access in tests. Live tests are opt-in (`UNIFI_CARTO_LIVE=1` + config) and never run in CI.

## Sources

| Layer | Base | Auth | Used for |
|---|---|---|---|
| Site Manager | `https://api.ui.com/v1/` | `X-API-Key: <site manager key>` (read-only) | discover hosts (consoles) and sites: `GET /v1/hosts`, `/v1/sites`; 10,000 req/min, honor 429 `Retry-After` |
| Official, via connector | `https://api.ui.com/v1/connector/consoles/{hostId}/network/integration/v1/` | Site Manager key | Integration API when no local URL is configured |
| Official, local | `https://<console>/proxy/network/integration/v1/` | `X-API-KEY: <network key>` | Integration API on LAN/VPN (preferred when configured) |
| Legacy, local | `https://<console>/proxy/network/` (`api/s/{site}/stat/device`, `rest/portconf`, `rest/networkconf`, `rest/wlanconf`, optional `stat/sta`) | first `X-API-KEY` (network key); on 401/403 fall back to local View-Only login if credentials are configured; else skip with a warning | port-level uplinks, port names, SFP, per-port VLANs, LLDP |

Legacy is **on by default when allowed** (user decision): attempted for every console that has a local URL; `--no-legacy` disables it. Legacy through the connector is not attempted in v0.1 (unverified); log once per console that port-level data needs a local URL.

Integration endpoints used (all GET, paginated `offset`/`limit` with `limit=200`, follow `totalCount`): `/v1/info` (application version), `/v1/sites`, `/v1/sites/{siteId}/devices`, `/v1/sites/{siteId}/devices/{deviceId}` (detail: ports, radios, uplink), `/v1/sites/{siteId}/networks` + `/networks/{id}`, `/v1/sites/{siteId}/wifi/broadcasts` + `/{id}`, `/v1/sites/{siteId}/wans`, `/v1/sites/{siteId}/switching/lags`, `/switching/mc-lag-domains`, `/switching/switch-stacks`, `/v1/sites/{siteId}/clients` (only with `--clients`). A 404 on an endpoint (older Network version) is recorded as a warning `endpoint_unavailable`, not an error.

## Architecture and ownership

```
src/unifi_carto/
  model.py        inventory model (pydantic)            [core, frozen]
  raw.py          raw snapshot containers (dataclasses)  [core, frozen]
  config.py       config file + secrets resolution      [core, frozen]
  errors.py       exception types                       [core, frozen]
  transport.py    Transport protocol (HTTP seam)         [core, frozen]
  http.py         ReadOnlyClient(Transport), TLS pin     [W1 transport]
  sitemanager.py  Site Manager discovery                 [W1 transport]
  integration.py  official API -> OfficialSnapshot       [W1 transport]
  legacy.py       legacy API -> LegacySnapshot           [W2 model]
  build.py        snapshots -> Inventory (pure)          [W2 model]
  scrub.py        fixture scrubber (pure)                [W1 transport]
  collect.py      orchestration across consoles/sites    [parent]
  cli.py          argparse CLI                           [parent]
  render/drawio.py  Inventory -> .drawio (pure)          [W3 render]
```

Seams: workers import only core modules plus their own; `build.py` and `render/` are pure functions over core types (no HTTP); `integration.py`/`legacy.py` return raw snapshots (lists of untouched JSON dicts) and never build model objects.

## Raw snapshots (`raw.py`)

`OfficialSnapshot(console: ConsoleRef, site: dict, app_version: str | None, devices: list[dict], device_details: dict[str, dict], networks: list[dict], wlans: list[dict], wans: list[dict], lags: list[dict], mc_lag_domains: list[dict], switch_stacks: list[dict], clients: list[dict] | None, warnings: list[CollectionWarning])`

`LegacySnapshot(site_name: str, devices: list[dict], portconfs: list[dict], networkconfs: list[dict], wlanconfs: list[dict], clients: list[dict] | None, auth: Literal["api_key", "login"], warnings: list[CollectionWarning])`

## Model (`model.py`, schema_version "1")

See `docs/MODEL.md` for field-by-field semantics. Key decisions:

- IDs: devices keyed by official UUID when present, else `mac:<aa:bb:..>`. All MACs normalized lowercase colon form. Joins across layers on MAC only.
- `Source` enum: `official`, `legacy`, `lldp`, `site_manager`, `inferred`. Every Device/Port/Link/Network/Wlan carries `sources: list[Source]` (sorted, unique). Links carry a single `source` for the edge itself.
- `Link.a` is the downstream end (the device whose uplink this is), `Link.b` the upstream. Ends are `LinkEnd(device_id: str | None, port_idx: int | None, external: ExternalPeer | None)`; exactly one of `device_id`/`external` is set. `kind`: `wired`, `wireless`, `mesh`, `unknown`.
- Official-only link: `a.device_id` + `b.device_id`, both `port_idx` None, `source=official`. Legacy enrichment fills `a.port_idx = uplink.port_idx`, `b.port_idx = uplink.uplink_remote_port`, `kind` from `uplink.type`, `speed_mbps`, and sets `source=legacy` (sources of both devices gain `legacy`). LLDP neighbors not matching a known device become links to `external` peers with `source=lldp`.
- LAG membership: `Lag(id, kind, members[{device_id, port_idxs}])` from official; ports referencing a LAG get `lag_id`. Parallel physical links of a LAG are separate `Link`s sharing `lag_id`.
- Per-port VLANs (legacy only): `native_vlan: int | None`, `tagged_vlans: list[int] | Literal["all"] | None`, `profile: str | None` resolved via portconf/networkconf. `None` means unknown, not "none".
- `Inventory(schema_version, generated_at, collector_version, consoles, sites, devices, links, lags, networks, wlans, wans, clients | None, warnings)`.
- JSON Schema exported to `schema/inventory-v1.schema.json` via `python -m unifi_carto.model > schema/...` and checked in CI for drift.

## CLI (`unifi-carto`)

```
unifi-carto collect [-c config.toml] [-o inventory.json] [--console NAME ...] [--site NAME ...] [--no-legacy] [--clients] [--debug]
unifi-carto render  inventory.json -o out.drawio [--pages logical,cables,schedule,rack-inventory]
unifi-carto pin URL                      # print SHA-256 fingerprint of the console's TLS leaf
unifi-carto capture [-c ...] -o fixtures/<version>/ [--console NAME]   # scrubbed raw responses for tests
unifi-carto schema                       # print JSON Schema
```

Exit codes: 0 ok; 1 partial (some console/site failed, inventory still written, warnings say why); 2 usage/config error; 3 nothing collected.

## Config (`config.toml`)

```toml
[site_manager]
api_key_env = "UNIFI_SM_API_KEY"        # optional; enables discovery + connector

[[console]]
name = "hq"                              # matches Site Manager host name, or standalone
host_id = "..."                          # optional; from discovery
local_url = "https://10.0.0.1"           # optional; enables local official + legacy
api_key_env = "UNIFI_HQ_API_KEY"         # local Network integration key (View-Only admin)
tls_fingerprint = "sha256:AB:CD:..."     # or ca_cert = "/path/ca.pem"
legacy_username_env = "UNIFI_HQ_RO_USER" # optional cookie fallback (local View-Only admin)
legacy_password_file = "~/.config/unifi-carto/hq.pass"
```

Discovered Site Manager consoles without a `[[console]]` entry are collected via the connector (official layer only).

## Render (draw.io) - follows the `drawio-diagrams` skill conventions

Pages: **Logical** (devices by role tier: gateway / aggregation / access / AP, links labeled with ports when known, LAGs bold, wireless/mesh dotted, hostname + mgmt IP under each icon, VLAN legend), **Cables** (port-level faceplates for switches with used ports colored by purpose; only links with both port indices), **Cable schedule** (HTML table of links: A device:port, B device:port, speed, media, LAG, VLANs), **Rack inventory** (table of devices with model, U-height where known from a model map, IP, firmware - rack elevation placement stays manual). Unknown port indices render as "?" and are counted in a page note so missing legacy data is visible.

## Testing

- Synthetic fixtures under `tests/fixtures/synthetic/` built from the spec field names (documentation IPs, fake MACs `02:00:00:..`). Real-console fixtures land later under `tests/fixtures/<network-version>/` via `capture` (scrubbed).
- Required tests per module listed in each brief; CI runs ruff, mypy, pytest on 3.11 and 3.13, plus schema drift check.

## Amendments

### Amendment 1 (wave 1 integration, 2026-10-13)

Decisions on worker-reported contract issues:

1. **Pagination:** request `limit=200`; servers may cap the page size (the v10.6.106 reference says typically 100). Follow `totalCount` and advance by `len(data)`.
2. **Device list vs detail shapes:** in list items `features` and `interfaces` are string arrays and there is no `uplink`; in the detail they are objects (`features.{gateway,switching,accessPoint}`, `interfaces.{ports,radios}`) and `uplink: {deviceId}` is present. The builder reads the detail and falls back to the list item.
3. **LAG sources:** official LAGs come from both the device detail (`features.switching.lags[] {id, portIdxs}`, scoped to that device) and `/switching/lags` (`members[] {deviceId, portIdxs}`); the member port list may be named `portIdxs` or `portIndexes`. MC-LAG domains map to `Lag(kind=multi_chassis)` with the peer-link ports as members; switch stacks map to `Lag(kind=switch_stack)` with members and empty `port_idxs`.
4. **Legacy login** posts to `<scheme://host:port>/api/auth/login` (the URL origin), not `base_url + path`.
5. **Tagged VLANs:** `tagged_vlan_mgmt=custom` = all network VLANs minus `excluded_networkconf_ids` minus the port's native VLAN.
6. **IDs for legacy-only objects:** networks and WLANs `legacy:<_id>`; LAGs seen only in legacy `<device_id>:lag<master port idx>`.
7. **`Console.legacy = "disabled"`** is set by `collect.py` after the build when `--no-legacy` is given.
8. **Official-only AP-to-AP links** stay `kind=unknown`: an AP parent does not prove a wireless mesh uplink (in-wall APs have wired downstream ports).
9. **Render page names:** canonical `inventory`; `rack-inventory` and `cable-schedule` are accepted aliases. No U-height map in v0.1.
10. **Proxies:** the HTTP client ignores proxy environment variables (`trust_env=False`) so credentials never go to an unexpected proxy. Revisit if a user needs an HTTPS proxy for api.ui.com.
11. **Discovered Site Manager hosts** that 404 on the Network integration path (e.g. Protect-only NVRs) produce `console_not_network` and do not make the run partial.

### Amendment 2 (review 1 decisions, 2026-10-13)

Findings in `docs/reviews/2026-10-review-1.md`; decisions the fix wave implements exactly:

1. **Third-party loggers** (`httpx`, `httpcore`, `hpack`, `urllib3`) are pinned to WARNING in `cli.main` regardless of `--debug`; `--debug` raises only `unifi_carto.*`. (SEC-01)
2. **Per-console isolation:** any `CartoError` (incl. `TlsPinMismatch`, config errors resolving one console's secrets excluded) while collecting one console, official or legacy, becomes a warning (`console_failed`, `legacy_error`) and the run continues; official data already collected for that console is kept. Exit code 1. (COL-01, COL-02)
3. **Site Manager failure** (key refused, api.ui.com unreachable) becomes warning `site_manager_failed`; configured local consoles are still collected; exit 1. (w4 proposal)
4. **Console identity:** a configured console binds to a Site Manager host by `host_id` first; by name only if exactly one host has that name, else warning `console_ambiguous` and no binding (and every same-named host is still collected via the connector under its own id). Duplicate `host_id` across `[[console]]` entries is a ConfigError. (COL-03)
5. **No secrets in reprs:** `Target.official_key` and any dataclass/client field holding a secret use `repr=False`. (SEC-03)
6. **`Wan.raw`** keeps only an allow-list of non-secret keys (id, name, type/interface/role/priority/enabled/state-like fields, IP config without credentials); everything else is dropped, and keys matching `scrub.drop_key` are dropped recursively as a backstop. (SEC-02)
7. **Pagination hardening:** `get_paginated` caps at 1000 pages, stops on a page identical to the previous page (hash), and drops non-object items with one DEBUG log line. (ROB-01, COL-04)
8. **`ca_cert`** that is missing, a directory or unreadable -> ConfigError at load/plan time. (TLS-01)
9. **Write-call scanner** becomes AST-based: fails on any `Call` whose function attribute is post/put/patch/delete/request/stream/send outside `http.py`, any string literal equal (case-insensitive) to POST/PUT/PATCH/DELETE outside `http.py`, and any import of `urllib.request`, `requests`, `http.client`, `aiohttp`, `socket` (except in `http.py`). (RO-01)
10. **Login is never retried** on 429/5xx/transport errors (one attempt, then AuthRefused/ApiError). The capability path check compares the full origin. (report-only observations)
11. **Server-supplied ids** interpolated into URL paths are percent-encoded (`quote(id, safe="")`). (report-only)
12. **Legacy auth error strings** live in one shared constant in `errors.py`: `api.err.LoginRequired`, `api.err.NoPermission`, `api.err.Invalid`, `api.err.Unauthorized` -> AuthRefused.
13. **Scrubber** (SCR-01..05): MACs in colon/dash/dot/bare-12-hex forms anywhere in strings, incl. `mac:` prefixes; identity keys by pattern (keys ending in `name`, `username`, `hostname`; `alias`, `desc*`, `description`, `domain*`, `label`, `ssid`, `essid`, `email`, `note*`, `comment*`) unless the value is in an enum allow-list (`model`, `type`, `version`, `media`, `connector`, `state`, `purpose`, ...); drop location keys (`geo*`, `lat*`, `lon*`/`lng`, `city`, `country*`, `address`, `isp*`, `timezone`); IPv6 and IPv4 inside longer strings (URLs, CIDRs, lists); public IPv4 mapping injective within a capture (sequential table keyed by first appearance order is acceptable since captures are single-run). (SCR-01..05)
14. **Capture console identity:** `console.json` gets positional id/name, `local_url` and `host_id` removed, model/firmware kept. (SCR-06)
15. **Builder dedup:** one cable seen as an uplink with unknown remote port and as upstream LLDP with unparseable port id is one Link; merge port numbers from both observations. (BLD-01)
16. **Renderer:** strip C0 control characters (except tab/newline handled as spaces) from every rendered string. (RND-01)
17. **Legacy-only `collect -o` into a missing directory:** create parent directories? No: fail early with ConfigError before collecting.

### Amendment 3 (fix-wave 1 integration, 2026-10-13)

1. Site Manager host ids contain `:`; `connector_base_url` percent-encodes everything except `:` (a legal path character) so the id reaches api.ui.com as documented. Other server-supplied ids are encoded with `safe=""`.
2. The write-call scanner also flags `getattr` with a computed or write-verb name and any `httpx` import outside `http.py` (accepted from f1).
3. `scrub.py` is deterministic per salt; the public-IPv4 table is per capture run (process), not a pure function of one value (accepted from f2).
4. A repeated pagination page raises `ApiError` (partial data is never returned silently).
5. Exports of an inline `Wan.raw` use an allow-list that is a best guess until real WAN responses are captured; revisit with live fixtures.

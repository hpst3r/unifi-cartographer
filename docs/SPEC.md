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

(none yet)

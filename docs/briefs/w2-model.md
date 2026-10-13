# Brief W2 - model: legacy.py, build.py, synthetic fixtures

Read `docs/WORKER.md`, `docs/SPEC.md`, `docs/MODEL.md`, `src/unifi_carto/{model,raw,transport,errors,config}.py` first. W1 is writing `http.py` (the real `Transport`) in parallel; you code against `transport.Transport` and test with fakes.

API references (WebFetch allowed): official spec `https://raw.githubusercontent.com/opastorello/unifi-api-docs/main/network/v10.6.106/openapi.json` for the official JSON shapes. Legacy shapes are undocumented: use the Art-of-WiFi UniFi-API-client (PHP, MIT) source, unpoller/unifi Go structs (`https://github.com/unpoller/unifi`, MIT: `devices.go`, `usw.go`, `uap.go`, `udm.go`, `clients.go`, `networks.go`) and enuno/unifi-mcp-server `src/tools/topology.py` (Apache-2.0) as field references. Do not copy code; field names are facts. Cite which source you used for each legacy field in a comment block at the top of `build.py`.

## Files you own

`src/unifi_carto/legacy.py`, `src/unifi_carto/build.py`, `tests/fixtures/synthetic/**`, `tests/test_legacy.py`, `tests/test_build.py`, `tests/conftest.py` (fixture loaders).

## legacy.py

- `class Legacy` taking a `Transport` whose base is `https://<console>/proxy/network`.
- `collect_site(site_name: str, *, include_clients: bool, credentials: tuple[str, str] | None) -> LegacySnapshot | None`:
  1. Try `GET /api/s/{site}/stat/device` with the transport as given (API key). On `AuthRefused`: if `credentials`, call `transport.login_legacy(*credentials)` and retry once (auth="login"); if that is refused, or no credentials, return `None` and the caller records a warning (raise nothing). Return the snapshot with `auth` set accordingly.
  2. Then `rest/portconf`, `rest/networkconf`, `rest/wlanconf` (each optional: `EndpointUnavailable`/`ApiError` -> warning in the snapshot, continue), and `stat/sta` only if `include_clients`.
  3. Unwrap the legacy envelope `{"meta": {"rc": "ok"}, "data": [...]}`; tolerate a bare list.
  4. Return a `(snapshot | None, warnings)`-equivalent: since the signature returns `LegacySnapshot | None`, expose refusal details via a second method or attribute `last_warnings: list[CollectionWarning]` - pick one, document it in `public_api`.
- Never log response bodies at INFO (they contain config); DEBUG may log paths and counts only.

## build.py (pure; no I/O)

`def build_inventory(official: Sequence[OfficialSnapshot], legacy: Mapping[tuple[str, str], LegacySnapshot], *, collector_version: str, generated_at: datetime, include_clients: bool, extra_warnings: Sequence[CollectionWarning] = ()) -> Inventory` where the legacy mapping key is `(console_id, official_site_id)`.

Implement exactly the mapping in `docs/MODEL.md`:
- Consoles (from `OfficialSnapshot.console` + `app_version`, `legacy` status from presence of a legacy snapshot and its `auth`), Sites (official `id`, `name`, `internalReference` when present).
- Devices: official first (detail object preferred over list item); role from `features`; ports from `interfaces.ports`; radios. Then enrich from legacy by MAC: port names, enable, media, SFP, is_uplink, poe fields where official lacks them, profile/native/tagged VLANs (resolve `portconf_id` -> portconf name and its `native_networkconf_id`/`tagged_vlan_mgmt`/`excluded_networkconf_ids` when the port itself does not override; port-level fields override profile fields; `tagged_vlan_mgmt`: `auto` -> "all", `block_all` -> [], `custom` -> all network VLANs minus excluded; untagged default network VLAN 1 when `native_networkconf_id` points at the default network with no `vlan`). Legacy-only devices (MAC not in official) are added with `id=mac:...` and `sources=[legacy]`. Every enriched object gains `Source.LEGACY` in `sources`.
- Links: one per device with an uplink. Official-only: parent ids, ports None, `source=official`. With legacy: `a.port_idx=uplink.port_idx`, `b` = device whose MAC == `uplink.uplink_mac` (or `uplink.uplink_device_name` lookup only as last resort -> `source=inferred`), `b.port_idx=uplink.uplink_remote_port`, `kind` from `uplink.type` (`wire` -> wired, `wireless` -> wireless; an AP with a wireless uplink to another AP -> mesh), `speed_mbps=uplink.speed`. If official parent and legacy parent disagree, prefer legacy and add warning `uplink_conflict`. LLDP: for each `lldp_table` entry, if `chassis_id` matches a known device MAC and no link already covers that (device, port) pair, add a `source=lldp` link; otherwise add an external-peer link. Deduplicate: the same physical cable seen from both ends (A's uplink and B's LLDP) must be one Link. LAG member links: set `lag_id` when either end port is a LAG member. Deterministic link ids as specified in MODEL.md; output lists sorted by id.
- LAGs from `lags`/`mc_lag_domains`/`switch_stacks` (type discriminator LOCAL / MULTI_CHASSIS / SWITCH_STACK); set `Port.lag_id` on member ports.
- Networks: official + legacy networkconf joined by name and VLAN (official `vlanId` == legacy `vlan`; the default network has no `vlan` in legacy and VLAN 1/none in official); subnet/purpose/dhcp from legacy. Wlans: official broadcasts + legacy wlanconf joined by name. WANs from official as `Wan(raw=...)`.
- Clients only when `include_clients`: official clients + legacy `stat/sta` by MAC (`sw_mac` -> uplink device, `sw_port`, `vlan`).
- Never raise on missing/odd legacy fields: skip the field, and for unparseable records add `CollectionWarning(code="parse_error")` naming the device MAC.

## Fixtures and tests

- `tests/fixtures/synthetic/site-a/official/*.json` and `.../legacy/*.json` describing one site: UDM-Pro gateway (port 11 SFP+ LAN), USW-Aggregation (agg-a), two USW-Pro-48-PoE access switches where acc-1 uplinks with a 2-port LAG (ports 49+50 to agg ports 1+2) and acc-2 with a single SFP+ link, AP-1 wired to acc-1 port 5, AP-2 meshed wirelessly to AP-1, an LLDP-only non-UniFi server on acc-2 port 10, networks Default/Staff(10)/Voice(20)/Guest(30), a port profile "AP Trunk" (native 1, tagged all) on the AP port, one port with `block_all`, one with `custom` excluding Guest. Field names must match the spec (official) and the cited legacy sources. Also an "official only" variant (no legacy) and a "legacy refused" variant.
- Tests (at least): official-only build gives parent links with `port_idx=None` and `source=official`; legacy build gives port-accurate links incl. both LAG members with `lag_id`, mesh link kind, LLDP external peer, dedup of the double-seen cable, VLAN resolution for all three tagged modes, `uplink_conflict` warning, legacy-only device, parse_error tolerance (delete random legacy fields from a copy and the build still succeeds), deterministic output (build twice -> identical JSON), `Inventory` validates and round-trips. Legacy collector tests: API-key success; 401 -> login -> success (auth="login"); 401 without credentials -> None + warning; envelope `rc: error` handling; clients only when requested.

## Gate

ruff format/check, mypy strict, pytest all green; commit. Report public API and any MODEL.md mapping you found to be wrong (as `contract_issues`, with evidence).

# unifi-cartographer

Read-only collector for UniFi Network consoles. It pulls devices, ports, uplinks, VLANs, SSIDs and link aggregation from the **official UniFi Network API** (with **Site Manager** discovery across consoles), fills the port-level gaps the official API doesn't cover from the legacy controller endpoints when the console allows it, and writes one documented JSON inventory with per-fact provenance. A separate renderer turns that inventory into editable draw.io diagrams (logical topology, port-level cable layout, cable schedule, rack inventory).

> Status: early development. Not yet tested against a live console.

## Design in one paragraph

The official Integration API reports each device's uplink as its parent device only (`uplink: {deviceId}`), with no port numbers, LLDP or per-port VLANs. That is enough for a logical diagram but not for cabling. When a console has a local URL configured, unifi-cartographer also reads `stat/device` and the `rest/portconf`/`networkconf` tables, read-only, to recover port-to-port links, port names, SFP modules and native/tagged VLANs, joins them to the official records by MAC, and tags every fact with where it came from. See [`docs/SPEC.md`](docs/SPEC.md) and [`docs/MODEL.md`](docs/MODEL.md).

## Safety

- GET only, enforced in the HTTP layer (the one exception is the optional legacy login POST).
- TLS is always verified: system trust, a CA file, or a pinned fingerprint for self-signed consoles (`unifi-carto pin https://console`).
- Use API keys created by a **View-Only** admin. Secrets come from environment variables or 0600 files, never the config file itself.
- Client devices are excluded unless you pass `--clients`.

## Prior art and references

- [enuno/unifi-mcp-server](https://github.com/enuno/unifi-mcp-server) (Apache-2.0): its `topology.py` showed the official-uplink + legacy-port merge this project builds on.
- [gitkodak/unifi-map](https://github.com/gitkodak/unifi-map) (AGPL-3.0): a read-only UniFi-to-draw.io/Graphviz renderer on the legacy and v2 endpoints.
- [opastorello/unifi-api-docs](https://github.com/opastorello/unifi-api-docs): daily mirror of Ubiquiti's published OpenAPI specs.
- [hyperb1iss/unifly](https://github.com/hyperb1iss/unifly) (Apache-2.0): reference for where fields live across API families.

No code is copied from these projects; see [`NOTICE`](NOTICE).

## License

AGPL-3.0-or-later. See [`LICENSE`](LICENSE).

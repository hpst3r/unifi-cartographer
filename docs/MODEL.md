# Inventory model (schema_version "1")

Source of truth: `src/unifi_carto/model.py`; JSON Schema: `schema/inventory-v1.schema.json`.

## Conventions

- **Unknown is `null`, never a guess.** `native_vlan: null` means "not known", not "no native VLAN". Renderers must show unknowns (e.g. `?`).
- **Provenance.** `sources` on devices, ports, networks, WLANs and clients lists every layer that contributed a field. A link has one `source`: the most specific layer that established it (`legacy` > `lldp` > `official` > `inferred`).
- **IDs.** Device ids are official UUIDs when known, else `mac:<mac>`. MACs are lowercase colon form. Link ids are deterministic: `<a.device_id>:<a.port_idx|?>-<b.device_id or ext:<chassis_id>>:<b.port_idx|?>`.
- **Direction.** `Link.a` is the downstream end (the device whose uplink this is; for LLDP, the local switch port), `Link.b` the upstream end.

## Field mapping (official / legacy)

| Model | Official (Integration v1) | Legacy (`stat/device`, `rest/*`) |
|---|---|---|
| Device.id | `id` | (`mac:` fallback) |
| Device.mac | `macAddress` | `mac` |
| Device.name / model / ip / firmware / state | `name`, `model`, `ipAddress`, `firmwareVersion`, `state` | `name`, `model`/`model_name`, `ip`, `version`, `state` (numeric) |
| Device.role | `features` keys: `gateway` > `switching` > `accessPoint` | `type`: ugw/udm/uxg -> gateway, usw -> switch, uap -> access_point |
| Device.uplink_device_id | `uplink.deviceId` | `uplink.uplink_mac` -> device by MAC |
| Port.idx / connector / speed / max / state / poe | `interfaces.ports[]`: `idx`, `connector`, `speedMbps`, `maxSpeedMbps`, `state`, `poe` | `port_table[]`: `port_idx`, `media`, `speed`, `up`, `poe_*` |
| Port.name / enabled / sfp / is_uplink | - | `name`, `enable`, `sfp_vendor`/`sfp_part`/`sfp_serial`, `is_uplink` |
| Port.profile / native_vlan / tagged_vlans | - | `portconf_id` -> `rest/portconf.name`; `native_networkconf_id` -> `networkconf.vlan`; `tagged_vlan_mgmt` (`auto` = all, `block_all` = [], `custom` = all minus `excluded_networkconf_ids`) |
| Port.lag_id | `switching/lags` members `{deviceId, portIdxs}` | `aggregated_by` / `lag_member` |
| Link (uplink) | parent only: `uplink.deviceId` | `uplink.port_idx` (a), `uplink.uplink_mac` + `uplink.uplink_remote_port` (b), `uplink.type` (`wire` / `wireless`), `uplink.speed` |
| Link (LLDP) | - | `lldp_table[]`: `local_port_idx`, `chassis_id`, `port_id`, `is_wired` |
| Network | `networks[]`: `id`, `name`, `vlanId`, `enabled`, `management` | `networkconf`: `purpose`, `vlan`, `ip_subnet`, `dhcpd_enabled` |
| Wlan | `wifi/broadcasts[]`: `id`, `name`, `enabled`, `network`, `securityConfiguration` | `wlanconf`: `name`, `enabled`, `networkconf_id`, `security`, `wlan_bands` |
| Lag | `switching/lags`, `mc-lag-domains`, `switch-stacks` | - |
| Client.switch_port | - | `stat/sta`: `sw_mac`, `sw_port` |

Legacy field names are undocumented and version-dependent; the builder treats every one as optional.

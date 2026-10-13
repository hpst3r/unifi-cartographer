"""Generate the end-to-end fixtures (Site Manager hosts + two consoles) from the synthetic site-a.

Run from the repo root after ``make_site_a.py``: ``uv run python tests/fixtures/e2e/make_e2e.py``.
The JSON files are checked in; this script documents how they were made. Synthetic data only.

  sitemanager/hosts.json   three Site Manager hosts: hq, branch, nvr (Protect-only, no Network)
  hq/                      site-a with distinctive SSIDs (so a scrubbing grep cannot hit enum words
                           like the networkconf purpose "guest")
  branch/                  site-a shifted to new UUIDs, MACs (02:00:00:01:..), IPs (10.1.x) and names;
                           reached through the connector, official layer only
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

E2E = Path(__file__).resolve().parent
SITE_A = E2E.parent / "synthetic" / "site-a"

HOST_IDS = {
    "hq": "0000000000000000000000000000000000000000000000000000000000e2e001:100000001",
    "branch": "0000000000000000000000000000000000000000000000000000000000e2e002:100000002",
    "nvr": "0000000000000000000000000000000000000000000000000000000000e2e003:100000003",
}
SSIDS = {"corp": "hq-corp-wlan", "guest": "hq-guest-wlan"}


def hosts() -> dict[str, Any]:
    def host(name: str, shortname: str, version: str) -> dict[str, Any]:
        return {
            "id": HOST_IDS[name],
            "type": "console",
            "ipAddress": "203.0.113.10",
            "reportedState": {"name": name, "hostname": name, "version": version, "hardware": {"shortname": shortname}},
        }

    return {
        "data": [host("hq", "UDMPRO", "4.1.13"), host("branch", "UCGMAX", "4.1.13"), host("nvr", "UNVR", "4.0.5")],
        "httpStatusCode": 200,
        "traceId": "trace-e2e",
    }


def rename_ssids(name: str, body: Any) -> Any:
    if name in ("wlans.json", "rest_wlanconf.json"):
        records = body["data"] if isinstance(body, dict) else body
        for rec in records:
            rec["name"] = SSIDS[rec["name"]]
    return body


def to_branch(text: str) -> str:
    """Shift every identifier of site-a so the branch console cannot collide with hq."""
    text = text.replace("-0000-4000-8000-", "-0001-4000-8000-")  # every site-a UUID
    text = text.replace("02:00:00:00:", "02:00:00:01:")
    text = re.sub(r"\b10\.0\.", "10.1.", text)
    text = text.replace("console-hq", HOST_IDS["branch"]).replace("Site A", "Site B")
    for ssid in SSIDS.values():
        text = text.replace(ssid, ssid.replace("hq-", "br-"))
    return re.sub(r'"(udm-pro|usw-agg-a|usw-acc-1|usw-acc-2|uap-1|uap-2|usw-lab)"', r'"br-\1"', text)


def main() -> None:
    (E2E / "sitemanager").mkdir(parents=True, exist_ok=True)
    (E2E / "sitemanager" / "hosts.json").write_text(json.dumps(hosts(), indent=2, sort_keys=True) + "\n")
    for console in ("hq", "branch"):
        shutil.rmtree(E2E / console, ignore_errors=True)
    for layer in ("official", "legacy"):
        for src in sorted((SITE_A / layer).glob("*.json")):
            body = rename_ssids(src.name, json.loads(src.read_text()))
            if src.name == "console.json":
                body.update(id=HOST_IDS["hq"])
            text = json.dumps(body, indent=2, sort_keys=True) + "\n"
            for console, out in (("hq", text), ("branch", to_branch(text))):
                if console == "branch" and src.name == "console.json":
                    out = json.dumps(
                        {"id": HOST_IDS["branch"], "name": "branch", "access": "connector", "model": "UCGMAX"},
                        indent=2,
                        sort_keys=True,
                    )
                    out += "\n"
                dest = E2E / console / layer / src.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(out)


if __name__ == "__main__":
    main()

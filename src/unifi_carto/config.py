"""Config file loading and secret resolution.

Secrets are referenced, never inlined: ``*_env`` names an environment variable,
``*_file`` names a file that must be mode 0600 or tighter.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from unifi_carto.errors import ConfigError


@dataclass(frozen=True)
class SecretRef:
    env: str | None = None
    file: Path | None = None

    def resolve(self, what: str) -> str | None:
        """Return the secret, None if not configured. Raises ConfigError if configured but unusable."""
        if self.env:
            value = os.environ.get(self.env)
            if not value:
                raise ConfigError(f"{what}: environment variable {self.env} is not set")
            return value.strip()
        if self.file:
            path = self.file.expanduser()
            try:
                st = path.stat()
            except OSError as exc:
                raise ConfigError(f"{what}: cannot read secret file {path}: {exc.strerror}") from None
            if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise ConfigError(f"{what}: secret file {path} must not be accessible by group/other (chmod 600)")
            return path.read_text(encoding="utf-8").strip()
        return None

    @property
    def configured(self) -> bool:
        return bool(self.env or self.file)


@dataclass(frozen=True)
class TlsConfig:
    fingerprint: str | None = None  # "sha256:AB:CD:..." (case/colons tolerant)
    ca_cert: Path | None = None

    def __post_init__(self) -> None:
        if self.fingerprint and self.ca_cert:
            raise ConfigError("tls_fingerprint and ca_cert are mutually exclusive")


@dataclass(frozen=True)
class ConsoleConfig:
    name: str
    host_id: str | None = None
    local_url: str | None = None
    api_key: SecretRef = field(default_factory=SecretRef)
    tls: TlsConfig = field(default_factory=TlsConfig)
    legacy_username: SecretRef = field(default_factory=SecretRef)
    legacy_password: SecretRef = field(default_factory=SecretRef)


@dataclass(frozen=True)
class Config:
    site_manager_key: SecretRef = field(default_factory=SecretRef)
    consoles: tuple[ConsoleConfig, ...] = ()


def _secret(table: dict[str, object], prefix: str) -> SecretRef:
    env = table.get(f"{prefix}_env")
    file = table.get(f"{prefix}_file")
    if f"{prefix}" in table:
        raise ConfigError(f"inline secret '{prefix}' is not allowed; use {prefix}_env or {prefix}_file")
    if env is not None and not isinstance(env, str):
        raise ConfigError(f"{prefix}_env must be a string")
    if file is not None and not isinstance(file, str):
        raise ConfigError(f"{prefix}_file must be a string")
    if env and file:
        raise ConfigError(f"{prefix}_env and {prefix}_file are mutually exclusive")
    return SecretRef(env=env or None, file=Path(file) if file else None)


_CONSOLE_KEYS = {
    "name",
    "host_id",
    "local_url",
    "api_key_env",
    "api_key_file",
    "tls_fingerprint",
    "ca_cert",
    "legacy_username_env",
    "legacy_username_file",
    "legacy_password_env",
    "legacy_password_file",
}


def load_config(path: Path | None) -> Config:
    """Load a TOML config. ``None`` returns an empty config (env-only use)."""
    if path is None:
        return Config()
    try:
        data = tomllib.loads(path.expanduser().read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc.strerror}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from None

    sm = data.get("site_manager", {})
    if not isinstance(sm, dict):
        raise ConfigError("[site_manager] must be a table")
    consoles: list[ConsoleConfig] = []
    names: set[str] = set()
    for raw in data.get("console", []):
        if not isinstance(raw, dict):
            raise ConfigError("[[console]] entries must be tables")
        unknown = set(raw) - _CONSOLE_KEYS - {"api_key", "legacy_username", "legacy_password"}
        if unknown:
            raise ConfigError(f"unknown console keys: {sorted(unknown)}")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError("every [[console]] needs a name")
        if name in names:
            raise ConfigError(f"duplicate console name {name!r}")
        names.add(name)
        local_url = raw.get("local_url")
        if local_url is not None and (not isinstance(local_url, str) or not local_url.startswith("https://")):
            raise ConfigError(f"console {name}: local_url must be an https:// URL")
        ca = raw.get("ca_cert")
        consoles.append(
            ConsoleConfig(
                name=name,
                host_id=raw.get("host_id") if isinstance(raw.get("host_id"), str) else None,
                local_url=local_url.rstrip("/") if local_url else None,
                api_key=_secret(raw, "api_key"),
                tls=TlsConfig(
                    fingerprint=raw.get("tls_fingerprint") if isinstance(raw.get("tls_fingerprint"), str) else None,
                    ca_cert=Path(ca).expanduser() if isinstance(ca, str) else None,
                ),
                legacy_username=_secret(raw, "legacy_username"),
                legacy_password=_secret(raw, "legacy_password"),
            )
        )
    return Config(site_manager_key=_secret(sm, "api_key"), consoles=tuple(consoles))

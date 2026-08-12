"""Configuration, resolved once at startup.

Two shapes are supported and they produce the same object graph:

* **Single cluster from the environment** — `CLICKHOUSE_HOST` and friends. The cluster
  is named `default`.
* **Several clusters from a profiles file** — `CLICKSCOPE_PROFILES=/path/to.json`.

Credentials never arrive as command-line arguments: an argument is visible in shell
history and in `ps` output to every other user on the box.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Mapping

from .secrets import SecretError, has_reference
from .secrets import resolve as resolve_reference


class ConfigError(RuntimeError):
    """Raised when the environment does not describe a usable connection."""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Limits:
    """Server-side caps applied to every query ClickScope executes.

    These are sent as ClickHouse settings, so they bind inside the server. Even a query
    that slips past our own parser is still bounded by these.
    """

    max_execution_time: int = 30
    max_result_rows: int = 10_000
    max_rows_to_read: int = 100_000_000
    max_bytes_to_read: int = 10_000_000_000  # 10 GB
    max_memory_usage: int = 4_000_000_000  # 4 GB
    max_threads: int = 4

    # LIMIT handling. default_limit is appended when a query has none; max_limit clamps
    # anything larger. Kept separate from max_result_rows so the SQL stays readable.
    default_limit: int = 100
    max_limit: int = 10_000

    @classmethod
    def from_env(cls) -> "Limits":
        return cls(
            max_execution_time=_env_int("CLICKSCOPE_MAX_EXECUTION_TIME", 30),
            max_result_rows=_env_int("CLICKSCOPE_MAX_RESULT_ROWS", 10_000),
            max_rows_to_read=_env_int("CLICKSCOPE_MAX_ROWS_TO_READ", 100_000_000),
            max_bytes_to_read=_env_int("CLICKSCOPE_MAX_BYTES_TO_READ", 10_000_000_000),
            max_memory_usage=_env_int("CLICKSCOPE_MAX_MEMORY_USAGE", 4_000_000_000),
            max_threads=_env_int("CLICKSCOPE_MAX_THREADS", 4),
            default_limit=_env_int("CLICKSCOPE_DEFAULT_LIMIT", 100),
            max_limit=_env_int("CLICKSCOPE_MAX_LIMIT", 10_000),
        )

    def merged(self, overrides: Mapping[str, Any] | None, where: str) -> "Limits":
        """Return a copy with `overrides` applied.

        Layering is env defaults → file-wide `limits` → per-cluster `limits`, so a
        production profile can be stricter than the box-wide default without repeating
        every field.
        """
        if not overrides:
            return self
        known = {f.name for f in fields(self)}
        unknown = sorted(set(overrides) - known)
        if unknown:
            raise ConfigError(
                f"{where}: unknown limit(s) {', '.join(unknown)}. "
                f"Valid limits are: {', '.join(sorted(known))}"
            )
        coerced: dict[str, int] = {}
        for key, value in overrides.items():
            try:
                coerced[key] = int(value)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{where}: limit {key} must be an integer") from exc
        return replace(self, **coerced)

    def describe(self) -> dict[str, int]:
        return {
            "max_execution_time_s": self.max_execution_time,
            "max_result_rows": self.max_result_rows,
            "max_rows_to_read": self.max_rows_to_read,
            "max_bytes_to_read": self.max_bytes_to_read,
            "default_limit": self.default_limit,
            "max_limit": self.max_limit,
        }


@dataclass(frozen=True)
class ClusterConfig:
    """Everything needed to reach one ClickHouse cluster."""

    name: str
    host: str
    port: int
    username: str
    database: str
    secure: bool
    verify: bool
    connect_timeout: int
    send_receive_timeout: int
    limits: Limits
    description: str = ""
    # repr=False so the password cannot leak into a traceback or a log line that
    # happens to format the config object.
    password: str = field(default="", repr=False)

    def describe(self) -> dict[str, object]:
        """Connection facts safe to show a caller. Never includes the password."""
        out: dict[str, object] = {
            "cluster": self.name,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "database": self.database,
            "secure": self.secure,
            "read_only": True,
            "limits": self.limits.describe(),
        }
        if self.description:
            out["description"] = self.description
        return out


@dataclass(frozen=True)
class AppConfig:
    clusters: dict[str, ClusterConfig]
    default_cluster: str
    audit_path: Path | None

    @classmethod
    def from_env(cls) -> "AppConfig":
        profiles_raw = os.environ.get("CLICKSCOPE_PROFILES", "").strip()
        audit_raw = os.environ.get("CLICKSCOPE_AUDIT_LOG", "").strip()
        audit_path = Path(audit_raw).expanduser() if audit_raw else None

        if profiles_raw:
            return cls._from_profiles(Path(profiles_raw).expanduser(), audit_path)
        return cls._from_single_env(audit_path)

    # -- single cluster from the environment --------------------------------------

    @classmethod
    def _from_single_env(cls, audit_path: Path | None) -> "AppConfig":
        host = os.environ.get("CLICKHOUSE_HOST", "").strip()
        if not host:
            raise ConfigError(
                "No cluster configured. Either set CLICKHOUSE_HOST (plus "
                "CLICKHOUSE_USER and CLICKHOUSE_PASSWORD_FILE or CLICKHOUSE_PASSWORD), "
                "or point CLICKSCOPE_PROFILES at a JSON profiles file."
            )

        secure = _env_bool("CLICKHOUSE_SECURE", False)
        cluster = ClusterConfig(
            name="default",
            host=host,
            port=_env_int("CLICKHOUSE_PORT", 8443 if secure else 8123),
            username=os.environ.get("CLICKHOUSE_USER", "default").strip(),
            password=_password_from_env(),
            database=os.environ.get("CLICKHOUSE_DATABASE", "default").strip(),
            secure=secure,
            verify=_env_bool("CLICKHOUSE_VERIFY", True),
            connect_timeout=_env_int("CLICKHOUSE_CONNECT_TIMEOUT", 10),
            send_receive_timeout=_env_int("CLICKHOUSE_SEND_RECEIVE_TIMEOUT", 300),
            limits=Limits.from_env(),
        )
        return cls({"default": cluster}, "default", audit_path)

    # -- several clusters from a profiles file -------------------------------------

    @classmethod
    def _from_profiles(cls, path: Path, audit_path: Path | None) -> "AppConfig":
        _refuse_if_writable_by_others(path)

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError(f"cannot read CLICKSCOPE_PROFILES at {path}: {exc}") from None
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from None

        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: top level must be an object")

        entries = raw.get("clusters")
        if not isinstance(entries, dict) or not entries:
            raise ConfigError(f"{path}: expected a non-empty 'clusters' object")

        base_limits = Limits.from_env().merged(raw.get("limits"), f"{path}: limits")

        clusters: dict[str, ClusterConfig] = {}
        for name, entry in entries.items():
            if not isinstance(entry, dict):
                raise ConfigError(f"{path}: cluster {name!r} must be an object")
            clusters[name] = _cluster_from_profile(name, entry, base_limits, path)

        default = raw.get("default") or next(iter(clusters))
        if default not in clusters:
            raise ConfigError(
                f"{path}: default cluster {default!r} is not defined. "
                f"Known clusters: {', '.join(sorted(clusters))}"
            )
        return cls(clusters, default, audit_path)


def _cluster_from_profile(
    name: str,
    entry: dict[str, Any],
    base_limits: Limits,
    path: Path,
) -> ClusterConfig:
    where = f"{path}: cluster {name}"

    def text(key: str, default: str = "") -> str:
        """A string field, with any ${scheme:arg} references expanded."""
        raw = entry.get(key, default)
        if raw is None:
            return default
        try:
            return resolve_reference(str(raw), f"{where}.{key}").strip()
        except SecretError as exc:
            raise ConfigError(str(exc)) from None

    def number(key: str, default: int) -> int:
        raw = entry.get(key, default)
        try:
            return int(resolve_reference(str(raw), f"{where}.{key}"))
        except SecretError as exc:
            raise ConfigError(str(exc)) from None
        except ValueError:
            raise ConfigError(f"{where}.{key} must be an integer, got {raw!r}") from None

    host = text("host")
    if not host:
        raise ConfigError(f"{where} has no host")

    secure = bool(entry.get("secure", False))
    return ClusterConfig(
        name=name,
        host=host,
        port=number("port", 8443 if secure else 8123),
        username=text("user") or text("username") or "default",
        password=_password_from_profile(name, entry, where),
        database=text("database") or "default",
        secure=secure,
        verify=bool(entry.get("verify", True)),
        connect_timeout=number("connect_timeout", 10),
        send_receive_timeout=number("send_receive_timeout", 300),
        limits=base_limits.merged(entry.get("limits"), f"{where} limits"),
        description=str(entry.get("description", "")),
    )


def _password_from_env() -> str:
    """File first, then env var.

    A file is preferable: it can be mode 0600, it does not appear in `docker inspect`
    or a process environment dump, and it is what Kubernetes and Docker secrets mount.
    """
    path_raw = os.environ.get("CLICKHOUSE_PASSWORD_FILE", "").strip()
    if path_raw:
        return _read_password_file(Path(path_raw).expanduser())
    # An empty password is legitimate on a dev cluster, so absence of the variable and
    # an explicitly empty value are treated the same.
    return os.environ.get("CLICKHOUSE_PASSWORD", "")


def _password_from_profile(name: str, entry: dict[str, Any], where: str) -> str:
    """Resolve a cluster password, which must be a reference and never a literal.

    Rejecting inline values is what makes a profiles file safe to commit: the file can
    then only ever say *where* the secret lives. A warning would not achieve that —
    people commit past warnings.
    """
    raw = entry.get("password")
    if raw is None:
        # No password at all is legitimate: a local dev server, or a user whose
        # credentials come from elsewhere entirely.
        return ""

    if not has_reference(raw):
        raise ConfigError(
            f"{where}.password must be a reference, not a literal value. "
            "Use ${env:VAR}, ${file:/path/to/secret} or ${cmd:...} so the profiles "
            "file holds no secrets. For example:\n"
            '  "password": "${env:PROD_CH_PASSWORD}"\n'
            '  "password": "${file:~/.config/clickscope/prod.pw}"\n'
            '  "password": "${cmd:vault kv get -field=password secret/ch-prod}"'
        )

    try:
        return resolve_reference(str(raw), f"{where}.password")
    except SecretError as exc:
        raise ConfigError(str(exc)) from None


def _read_password_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"cannot read password file {path}: {exc}") from None


def _refuse_if_writable_by_others(path: Path) -> None:
    """Refuse a profiles file that anyone but the owner can write.

    A ${cmd:...} reference means the file decides what gets executed, so its integrity
    matters more than its confidentiality. SSH refuses to use a private key on the same
    grounds, and for the same reason.
    """
    if os.name == "nt":
        # Windows synthesizes st_mode as 0o666 for any writable file, so the POSIX
        # group/other bits carry no access-control meaning there — testing them would
        # refuse every file. Doing this properly needs ACL inspection and a pywin32
        # dependency, which is not worth it for a guard that is defence in depth.
        return
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise ConfigError(f"cannot read CLICKSCOPE_PROFILES at {path}: {exc}") from None
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError(
            f"{path} is writable by other users, so it cannot be trusted to name "
            f"commands or secret locations. Run: chmod go-w {path}"
        )

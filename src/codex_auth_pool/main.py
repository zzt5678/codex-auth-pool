#!/usr/bin/env python3
"""Import cliproxyapi Codex auth files into Codex Desktop auth format.

This script is intentionally conservative:
- `list` shows available cliproxyapi auth profiles.
- `preview` converts a profile and prints a redacted view.
- `export` writes the converted auth payload to a chosen file.
- `apply` backs up the current Codex auth file and then replaces it.

The goal is to reuse existing Codex/ChatGPT login tokens without forcing a
fresh login for every account.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil as shutil_lib
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import time
import plistlib


DEFAULT_CLIPROXY_DIR = Path.home() / ".cli-proxy-api"
DEFAULT_CODEX_AUTH_PATH = Path.home() / ".codex" / "cache" / "auth.json"
DEFAULT_CODEX_ROOT_AUTH_PATH = Path.home() / ".codex" / "auth.json"
DEFAULT_CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
DEFAULT_EXPORT_DIR = Path.home() / ".codex-auth-pool" / "exports"
DEFAULT_MANAGED_DIR = Path.home() / ".codex-auth-pool" / "profiles"
DEFAULT_SOURCE_META_DIR = Path.home() / ".codex-auth-pool" / "source-meta"
DEFAULT_STATE_PATH = Path.home() / ".codex-auth-pool" / "state.json"
DEFAULT_CONFIG_PATH = Path.home() / ".codex-auth-pool" / "config.json"
DEFAULT_EVENTS_PATH = Path.home() / ".codex-auth-pool" / "events.jsonl"
DEFAULT_ENV_SNAPSHOTS_DIR = Path.home() / ".codex-auth-pool" / "env-snapshots"
DEFAULT_CODEX_APP = Path("/Applications/Codex.app")
DEFAULT_LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
DEFAULT_LAUNCHD_LABEL = "ai.codex.auth.pool"
DEFAULT_LAUNCHD_STDOUT = Path.home() / ".codex-auth-pool" / "logs" / "launchd.stdout.log"
DEFAULT_LAUNCHD_STDERR = Path.home() / ".codex-auth-pool" / "logs" / "launchd.stderr.log"
BACKUP_TIMESTAMP_FMT = "%Y%m%d-%H%M%S"
ENV_VAR_MAP = {
    "source_dir": "CODEX_AUTH_POOL_SOURCE_DIR",
    "managed_dir": "CODEX_AUTH_POOL_MANAGED_DIR",
    "state_path": "CODEX_AUTH_POOL_STATE_PATH",
    "config_path": "CODEX_AUTH_POOL_CONFIG_PATH",
    "events_path": "CODEX_AUTH_POOL_EVENTS_PATH",
    "env_snapshots_dir": "CODEX_AUTH_POOL_ENV_SNAPSHOTS_DIR",
    "target": "CODEX_AUTH_POOL_TARGET",
    "sessions_dir": "CODEX_AUTH_POOL_SESSIONS_DIR",
    "app_path": "CODEX_AUTH_POOL_APP_PATH",
}
DEFAULT_ARG_VALUES = {
    "source_dir": DEFAULT_CLIPROXY_DIR,
    "managed_dir": DEFAULT_MANAGED_DIR,
    "state_path": DEFAULT_STATE_PATH,
    "config_path": DEFAULT_CONFIG_PATH,
    "events_path": DEFAULT_EVENTS_PATH,
    "env_snapshots_dir": DEFAULT_ENV_SNAPSHOTS_DIR,
    "target": str(DEFAULT_CODEX_AUTH_PATH),
    "sessions_dir": DEFAULT_CODEX_SESSIONS_DIR,
    "app_path": str(DEFAULT_CODEX_APP),
}
REQUIRED_SOURCE_KEYS = (
    "access_token",
    "refresh_token",
    "id_token",
    "account_id",
)
DEFAULT_PRIMARY_THRESHOLD = 95.0
DEFAULT_SECONDARY_THRESHOLD = 98.0


@dataclass
class ProfileSummary:
    path: Path
    source_kind: str
    email: str
    account_id: str
    weekly_reset_at: str | None
    last_refresh: str | None
    expired: str | None
    disabled: bool


@dataclass
class RateLimitSnapshot:
    primary_used_percent: float | None
    primary_resets_at: datetime | None
    primary_window_minutes: int | None
    secondary_used_percent: float | None
    secondary_resets_at: datetime | None
    secondary_window_minutes: int | None
    plan_type: str | None
    source_file: Path
    event_timestamp: str | None


@dataclass
class RemoteUsageSnapshot:
    account_id: str | None
    email: str | None
    plan_type: str | None
    allowed: bool | None
    limit_reached: bool | None
    primary_used_percent: float | None
    primary_reset_at: datetime | None
    primary_window_seconds: int | None
    secondary_used_percent: float | None
    secondary_reset_at: datetime | None
    secondary_window_seconds: int | None
    fetched_at: datetime
    source: str


def now_local() -> datetime:
    return datetime.now().astimezone()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_unix_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone()
    except (TypeError, ValueError, OSError):
        return None


def iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise SystemExit(f"file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid json in {path}: {exc}") from exc


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"profiles": {}, "current_profile": None}
    data = read_json(path)
    if "profiles" not in data:
        data["profiles"] = {}
    if "current_profile" not in data:
        data["current_profile"] = None
    return data


def save_state(path: Path, payload: dict[str, Any]) -> None:
    write_json(path, payload)


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return read_json(path)


def save_config(path: Path, payload: dict[str, Any]) -> None:
    write_json(path, payload)


def _coerce_arg_value(key: str, value: Any) -> Any:
    path_keys = {
        "source_dir",
        "managed_dir",
        "state_path",
        "config_path",
        "events_path",
        "env_snapshots_dir",
        "sessions_dir",
    }
    if key in path_keys:
        return Path(str(value)).expanduser()
    return str(value)


def apply_runtime_defaults(args: argparse.Namespace) -> argparse.Namespace:
    env_config_path = os.environ.get(ENV_VAR_MAP["config_path"])
    config_path = (
        Path(env_config_path).expanduser()
        if env_config_path
        else Path(str(getattr(args, "config_path", DEFAULT_CONFIG_PATH))).expanduser()
    )
    args.config_path = config_path
    config = load_config(config_path)

    for key, env_name in ENV_VAR_MAP.items():
        current = getattr(args, key, None)
        default = DEFAULT_ARG_VALUES.get(key)
        env_value = os.environ.get(env_name)
        config_value = config.get(key)

        if current != default:
            continue
        if env_value:
            setattr(args, key, _coerce_arg_value(key, env_value))
        elif config_value is not None:
            setattr(args, key, _coerce_arg_value(key, config_value))
    return args


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def append_event(events_path: Path, event_type: str, **fields: Any) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": now_local().isoformat(),
        "event_type": event_type,
        **fields,
    }
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def send_notification(title: str, message: str, enabled: bool = True) -> None:
    if not enabled:
        return
    script = f'display notification "{message.replace(chr(34), chr(39))}" with title "{title.replace(chr(34), chr(39))}"'
    run_command(["osascript", "-e", script])


def env_snapshot_items() -> list[tuple[str, Path]]:
    return [
        ("config.toml", Path.home() / ".codex" / "config.toml"),
        ("plugins", Path.home() / ".codex" / "plugins"),
        ("cache_codex_apps_tools", Path.home() / ".codex" / "cache" / "codex_apps_tools"),
        ("tmp_bundled_marketplaces", Path.home() / ".codex" / ".tmp" / "bundled-marketplaces"),
        ("tmp_marketplaces", Path.home() / ".codex" / ".tmp" / "marketplaces"),
    ]


def snapshot_manifest(snapshot_dir: Path) -> Path:
    return snapshot_dir / "manifest.json"


def meta_path_for_profile(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve()
        source_root = DEFAULT_CLIPROXY_DIR.expanduser().resolve()
        if resolved.is_relative_to(source_root):
            return DEFAULT_SOURCE_META_DIR / f"{path.stem}.meta.json"
    except FileNotFoundError:
        pass
    return path.with_suffix(".meta.json")


def legacy_meta_path_for_profile(path: Path) -> Path:
    return path.with_suffix(".meta.json")


def copy_item(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if dst.exists():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def format_bytes(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num_bytes)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{num_bytes}B"


def fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.strftime("%Y-%m-%d %H:%M:%S %Z")


def fmt_percent(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.0f}%"


def fmt_timedelta_until(value: datetime | None) -> str:
    if value is None:
        return "-"
    delta = value - now_local()
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "now"
    hours, rem = divmod(seconds, 3600)
    minutes, _ = divmod(rem, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def read_recent_event(events_path: Path) -> dict[str, Any] | None:
    if not events_path.exists():
        return None
    lines = events_path.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def read_recent_log_line(path: Path) -> str | None:
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    for line in reversed(lines):
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def file_mtime(path: Path) -> datetime | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).astimezone()


def current_account_summary(target_path: Path, source_dir: Path, managed_dir: Path) -> ProfileSummary | None:
    current_account = current_auth_account_id(target_path)
    if not current_account:
        return None
    for path in discover_all_profiles(source_dir, managed_dir):
        summary = summarize_profile(path)
        if summary.account_id == current_account:
            return summary
    return None


def list_env_snapshots(env_snapshots_dir: Path) -> list[Path]:
    if not env_snapshots_dir.exists():
        return []
    snapshots = [
        path
        for path in env_snapshots_dir.iterdir()
        if path.is_dir() and snapshot_manifest(path).exists()
    ]
    snapshots.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return snapshots


def _snapshot_name(name: str | None, prefix: str = "snapshot") -> str:
    if name:
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name.strip())
        if safe:
            return safe
    return f"{prefix}-{datetime.now().strftime(BACKUP_TIMESTAMP_FMT)}"


def create_env_snapshot(
    env_snapshots_dir: Path,
    *,
    name: str | None = None,
    note: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    snapshot_dir = env_snapshots_dir / _snapshot_name(name)
    if snapshot_dir.exists():
        raise SystemExit(f"snapshot already exists: {snapshot_dir}")
    snapshot_dir.mkdir(parents=True, exist_ok=False)

    manifest: dict[str, Any] = {
        "created_at": now_local().isoformat(),
        "note": note,
        "items": [],
    }

    for item_name, source_path in env_snapshot_items():
        item_record = {
            "name": item_name,
            "source_path": str(source_path),
            "exists_at_capture": source_path.exists(),
            "size_bytes": dir_size(source_path),
            "stored_path": None,
            "type": None,
        }
        if source_path.exists():
            destination = snapshot_dir / item_name
            copy_item(source_path, destination)
            item_record["stored_path"] = str(destination)
            item_record["type"] = "directory" if source_path.is_dir() else "file"
        manifest["items"].append(item_record)

    write_json(snapshot_manifest(snapshot_dir), manifest)
    return snapshot_dir, manifest


def load_snapshot_manifest(snapshot_dir: Path) -> dict[str, Any]:
    manifest_path = snapshot_manifest(snapshot_dir)
    if not manifest_path.exists():
        raise SystemExit(f"snapshot manifest not found: {manifest_path}")
    return read_json(manifest_path)


def restore_env_snapshot(snapshot_dir: Path) -> dict[str, Any]:
    manifest = load_snapshot_manifest(snapshot_dir)
    restored: list[dict[str, Any]] = []
    for item in manifest.get("items", []):
        source_path = item.get("stored_path")
        target_path = item.get("source_path")
        if not source_path or not target_path:
            continue
        source = Path(str(source_path))
        target = Path(str(target_path))
        if not source.exists():
            continue
        copy_item(source, target)
        restored.append(
            {
                "name": item.get("name"),
                "target_path": str(target),
                "size_bytes": dir_size(source),
            }
        )
    return {
        "snapshot_dir": str(snapshot_dir),
        "restored_items": restored,
    }


def discover_profiles(source_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in source_dir.glob("codex-*.json")
        if path.is_file()
        and ".bak-" not in path.name
        and ".backup-" not in path.name
        and not path.name.endswith(".meta.json")
    )


def discover_managed_profiles(managed_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in managed_dir.glob("*.json")
        if path.is_file()
        and ".bak-" not in path.name
        and ".backup-" not in path.name
        and not path.name.endswith(".meta.json")
    )


def discover_all_profiles(source_dir: Path, managed_dir: Path) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for path in discover_profiles(source_dir) + discover_managed_profiles(managed_dir):
        if path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


def _jwt_payload(token: str | None) -> dict[str, Any]:
    if not token or token.count(".") < 2:
        return {}
    try:
        payload_part = token.split(".")[1]
        payload_part += "=" * (-len(payload_part) % 4)
        raw = base64.urlsafe_b64decode(payload_part)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def read_profile_metadata(path: Path) -> dict[str, Any]:
    meta_path = meta_path_for_profile(path)
    if not meta_path.exists():
        legacy = legacy_meta_path_for_profile(path)
        if legacy != meta_path and legacy.exists():
            return read_json(legacy)
        return {}
    return read_json(meta_path)


def parse_status_message(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return {}


def normalize_source_payload(path: Path, payload: dict[str, Any], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = metadata or {}
    if all(key in payload for key in REQUIRED_SOURCE_KEYS):
        normalized = dict(payload)
        normalized.setdefault("source_kind", "cliproxyapi")
        normalized.setdefault("source_file", str(path))
        status_message = parse_status_message(payload.get("status_message"))
        error = status_message.get("error", {}) if isinstance(status_message, dict) else {}
        reset_at = parse_unix_ts(error.get("resets_at"))
        if reset_at is not None:
            normalized["limit_reset_at"] = reset_at.isoformat()
            normalized["limit_reason"] = error.get("type")
        return normalized

    tokens = payload.get("tokens", {})
    if isinstance(tokens, dict) and all(key in tokens for key in REQUIRED_SOURCE_KEYS):
        claims = _jwt_payload(tokens.get("id_token"))
        normalized = {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "id_token": tokens["id_token"],
            "account_id": tokens["account_id"],
            "email": (
                claims.get("email")
                or metadata.get("email")
                or payload.get("email")
                or ""
            ),
            "last_refresh": payload.get("last_refresh") or metadata.get("last_refresh"),
            "weekly_reset_at": metadata.get("weekly_reset_at") or payload.get("weekly_reset_at"),
            "expired": metadata.get("expired") or payload.get("expired"),
            "disabled": bool(metadata.get("disabled", payload.get("disabled", False))),
            "type": metadata.get("type", payload.get("type", "codex")),
            "source_kind": metadata.get("source_kind", payload.get("source_kind", "managed")),
            "source_file": metadata.get("source_file", str(path)),
            "limit_reset_at": metadata.get("limit_reset_at"),
            "limit_reason": metadata.get("limit_reason"),
        }
        return normalized

    raise SystemExit(f"unsupported auth profile format: {path}")


def source_rank(source_kind: str) -> int:
    ranks = {
        "managed": 0,
        "manual": 1,
        "cliproxyapi": 2,
    }
    return ranks.get(source_kind, 9)


def managed_profile_name(payload: dict[str, Any], explicit_name: str | None = None) -> str:
    if explicit_name:
        base = explicit_name.strip()
    else:
        email = str(payload.get("email", "")).strip()
        account_id = str(payload.get("account_id", "")).strip()
        base = email or account_id or f"profile-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    safe = "".join(ch if ch.isalnum() or ch in ".@_-" else "_" for ch in base)
    if not safe.endswith(".json"):
        safe += ".json"
    return safe


def summarize_profile(path: Path) -> ProfileSummary:
    payload = normalize_source_payload(path, read_json(path), read_profile_metadata(path))
    return ProfileSummary(
        path=path,
        source_kind=str(payload.get("source_kind", "unknown")),
        email=str(payload.get("email", "")),
        account_id=str(payload.get("account_id", "")),
        weekly_reset_at=payload.get("weekly_reset_at"),
        last_refresh=payload.get("last_refresh"),
        expired=payload.get("expired"),
        disabled=bool(payload.get("disabled", False)),
    )


def pick_profile(source_dir: Path, managed_dir: Path, selector: str) -> Path:
    candidate = Path(selector).expanduser()
    if candidate.exists():
        return candidate

    named = source_dir / selector
    if named.exists():
        return named
    managed_named = managed_dir / selector
    if managed_named.exists():
        return managed_named

    managed_exact = list(managed_dir.glob(f"{selector}*.json"))
    managed_exact = [path for path in managed_exact if path.is_file() and not path.name.endswith(".meta.json")]
    if len(managed_exact) == 1:
        return managed_exact[0]

    matches = []
    for path in discover_all_profiles(source_dir, managed_dir):
        normalized = normalize_source_payload(path, read_json(path), read_profile_metadata(path))
        if (
            selector in path.name
            or selector in path.stem
            or selector in str(normalized.get("email", ""))
        ):
            matches.append(path)
    if not matches:
        raise SystemExit(
            f"no profile matched '{selector}' under {source_dir} or {managed_dir}. "
            "Run `list` first."
        )
    deduped_by_account: dict[str, Path] = {}
    for path in matches:
        normalized = normalize_source_payload(path, read_json(path), read_profile_metadata(path))
        account_id = str(normalized.get("account_id", ""))
        existing = deduped_by_account.get(account_id)
        if existing is None or path.parent == managed_dir:
            deduped_by_account[account_id] = path
    matches = list(deduped_by_account.values())
    if len(matches) > 1:
        joined = "\n".join(f"- {path.name}" for path in matches)
        raise SystemExit(f"multiple profiles matched '{selector}':\n{joined}")
    return matches[0]


def convert_profile(source_payload: dict[str, Any]) -> dict[str, Any]:
    source_payload = normalize_source_payload(
        Path(str(source_payload.get("source_file", "<memory>"))),
        source_payload,
    )
    missing = [key for key in REQUIRED_SOURCE_KEYS if not source_payload.get(key)]
    if missing:
        raise SystemExit(f"source profile is missing required keys: {', '.join(missing)}")

    tokens = {
        "id_token": source_payload["id_token"],
        "access_token": source_payload["access_token"],
        "refresh_token": source_payload["refresh_token"],
        "account_id": source_payload["account_id"],
    }
    converted = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": tokens,
        "last_refresh": source_payload.get("last_refresh"),
    }
    return converted


def metadata_for_profile(normalized: dict[str, Any], auth_path: Path) -> dict[str, Any]:
    return {
        "email": normalized.get("email", ""),
        "account_id": normalized.get("account_id", ""),
        "weekly_reset_at": normalized.get("weekly_reset_at"),
        "last_refresh": normalized.get("last_refresh"),
        "expired": normalized.get("expired"),
        "disabled": bool(normalized.get("disabled", False)),
        "type": normalized.get("type", "codex"),
        "source_kind": normalized.get("source_kind", "managed"),
        "source_file": normalized.get("source_file", str(auth_path)),
        "limit_reset_at": normalized.get("limit_reset_at"),
        "limit_reason": normalized.get("limit_reason"),
        "observed_account_id": normalized.get("observed_account_id"),
        "observed_email": normalized.get("observed_email"),
        "observed_plan_type": normalized.get("observed_plan_type"),
        "observed_allowed": normalized.get("observed_allowed"),
        "observed_limit_reached": normalized.get("observed_limit_reached"),
        "observed_primary_used_percent": normalized.get("observed_primary_used_percent"),
        "observed_primary_reset_at": normalized.get("observed_primary_reset_at"),
        "observed_primary_window_seconds": normalized.get("observed_primary_window_seconds"),
        "observed_secondary_used_percent": normalized.get("observed_secondary_used_percent"),
        "observed_secondary_reset_at": normalized.get("observed_secondary_reset_at"),
        "observed_secondary_window_seconds": normalized.get("observed_secondary_window_seconds"),
        "usage_checked_at": normalized.get("usage_checked_at"),
        "weekly_reset_source": normalized.get("weekly_reset_source"),
        "usage_source": normalized.get("usage_source"),
    }


def update_profile_metadata(path: Path, **updates: Any) -> dict[str, Any]:
    meta = read_profile_metadata(path)
    if not meta:
        normalized = normalize_source_payload(path, read_json(path))
        meta = metadata_for_profile(normalized, path)
    for key, value in updates.items():
        if value is None and key in meta:
            meta.pop(key, None)
        elif value is not None:
            meta[key] = value
    write_json(meta_path_for_profile(path), meta)
    return meta


def current_auth_account_id(target_path: Path) -> str | None:
    if not target_path.exists():
        return None
    try:
        payload = read_json(target_path)
    except SystemExit:
        return None
    tokens = payload.get("tokens", {})
    return tokens.get("account_id")


def current_auth_email(target_path: Path) -> str | None:
    if not target_path.exists():
        return None
    try:
        payload = read_json(target_path)
    except SystemExit:
        return None
    tokens = payload.get("tokens", {})
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str):
        return None
    # We do not fully decode JWT here; email is not required for core logic.
    return None


def redact(value: str | None, keep: int = 16) -> str | None:
    if value is None:
        return None
    if len(value) <= keep:
        return value
    return f"{value[:keep]}..."


def redacted_payload(payload: dict[str, Any]) -> dict[str, Any]:
    cloned = json.loads(json.dumps(payload))
    tokens = cloned.get("tokens", {})
    for key in ("id_token", "access_token", "refresh_token"):
        if key in tokens:
            tokens[key] = redact(tokens[key], keep=24)
    return cloned


def export_path_for(source_path: Path, export_dir: Path) -> Path:
    stem = source_path.stem
    return export_dir / f"{stem}.codex-auth.json"


def make_backup(path: Path) -> Path:
    ts = datetime.now().strftime(BACKUP_TIMESTAMP_FMT)
    backup = path.with_name(f"{path.stem}.backup-{ts}{path.suffix}")
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    return backup


def sync_secondary_auth_files(primary_payload: dict[str, Any], primary_target: Path) -> list[Path]:
    synced: list[Path] = []
    secondary_targets = [DEFAULT_CODEX_ROOT_AUTH_PATH]
    for target in secondary_targets:
        if target == primary_target:
            continue
        if target.exists():
            make_backup(target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
        write_json(target, primary_payload)
        synced.append(target)
    return synced


def restart_codex_app(app_path: Path, hard: bool = True, wait_seconds: float = 2.0) -> None:
    if hard:
        run_command(["pkill", "-x", "Codex"])
        run_command(["pkill", "-f", "/Applications/Codex.app/Contents/Frameworks/Codex Helper"])
        run_command(["pkill", "-f", "SkyComputerUseClient"])
        time.sleep(wait_seconds)
    else:
        run_command(["osascript", "-e", 'tell application "Codex" to quit'])
        time.sleep(wait_seconds)

    completed = run_command(["open", "-a", str(app_path)])
    if completed.returncode != 0:
        raise SystemExit(f"failed to open Codex app:\n{completed.stderr or completed.stdout}")


def launchd_plist_path(label: str) -> Path:
    return DEFAULT_LAUNCH_AGENTS_DIR / f"{label}.plist"


def launchctl_domain() -> str:
    import os

    return f"gui/{os.getuid()}"


def launchctl_target(label: str) -> str:
    return f"{launchctl_domain()}/{label}"


def write_launchd_plist(
    *,
    label: str,
    command_path: str,
    stdout_path: Path,
    stderr_path: Path,
    interval_seconds: int,
    state_path: Path,
    target: str,
    sessions_dir: Path,
    source_dir: Path,
    managed_dir: Path,
    events_path: Path,
    primary_threshold: float,
    secondary_threshold: float,
    restart_after_switch: bool,
    app_path: Path,
    refresh_usage: bool,
    usage_max_age_minutes: int,
) -> Path:
    plist_path = launchd_plist_path(label)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": label,
        "ProgramArguments": [
            command_path,
            "--state-path",
            str(state_path),
            "--target",
            str(target),
            "--events-path",
            str(events_path),
            "--sessions-dir",
            str(sessions_dir),
            "--source-dir",
            str(source_dir),
            "--managed-dir",
            str(managed_dir),
            "--app-path",
            str(app_path),
            "daemon",
            "--interval-seconds",
            str(interval_seconds),
            "--primary-threshold",
            str(primary_threshold),
            "--secondary-threshold",
            str(secondary_threshold),
            "--usage-max-age-minutes",
            str(usage_max_age_minutes),
        ]
        + (["--restart-after-switch"] if restart_after_switch else [])
        + (["--refresh-usage"] if refresh_usage else []),
        "WorkingDirectory": str(Path.home()),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
        },
    }
    with plist_path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)
    return plist_path


def launchctl_bootout(label: str) -> None:
    run_command(["launchctl", "bootout", launchctl_target(label)])


def launchctl_bootstrap(label: str, plist_path: Path) -> None:
    completed = run_command(["launchctl", "bootstrap", launchctl_domain(), str(plist_path)])
    if completed.returncode != 0:
        raise SystemExit(f"failed to bootstrap {label}:\n{completed.stderr or completed.stdout}")


def launchctl_kickstart(label: str) -> None:
    completed = run_command(["launchctl", "kickstart", "-k", launchctl_target(label)])
    if completed.returncode != 0:
        raise SystemExit(f"failed to kickstart {label}:\n{completed.stderr or completed.stdout}")


def launchctl_status(label: str) -> dict[str, Any]:
    plist_path = launchd_plist_path(label)
    status = {
        "label": label,
        "plist_path": str(plist_path),
        "installed": plist_path.exists(),
        "loaded": False,
        "state": None,
        "pid": None,
    }
    completed = run_command(["launchctl", "print", launchctl_target(label)])
    if completed.returncode != 0:
        return status
    status["loaded"] = True
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("state ="):
            status["state"] = line.split("=", 1)[1].strip()
        elif line.startswith("pid ="):
            status["pid"] = line.split("=", 1)[1].strip()
    return status


def profile_record(state: dict[str, Any], account_id: str) -> dict[str, Any]:
    profiles = state.setdefault("profiles", {})
    return profiles.setdefault(account_id, {})


def cooldown_until(state: dict[str, Any], account_id: str) -> datetime | None:
    record = state.get("profiles", {}).get(account_id, {})
    return parse_dt(record.get("cooldown_until"))


def cooldown_reason(state: dict[str, Any], account_id: str) -> str | None:
    record = state.get("profiles", {}).get(account_id, {})
    return record.get("cooldown_reason")


def effective_weekly_reset(summary: ProfileSummary) -> datetime:
    weekly = parse_dt(summary.weekly_reset_at)
    if weekly is not None:
        return weekly
    expired = parse_dt(summary.expired)
    if expired is not None:
        return expired
    return datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


def preferred_next_account_id(state: dict[str, Any]) -> str | None:
    value = state.get("preferred_next_account_id")
    return str(value) if value else None


def limit_reset_at_for_profile(path: Path) -> datetime | None:
    meta = read_profile_metadata(path)
    return parse_dt(meta.get("limit_reset_at"))


def limit_reason_for_profile(path: Path) -> str | None:
    meta = read_profile_metadata(path)
    value = meta.get("limit_reason")
    return str(value) if value else None


def usage_checked_at_for_profile(path: Path) -> datetime | None:
    meta = read_profile_metadata(path)
    return parse_dt(meta.get("usage_checked_at"))


def observed_secondary_reset_at_for_profile(path: Path) -> datetime | None:
    meta = read_profile_metadata(path)
    return parse_dt(meta.get("observed_secondary_reset_at"))


def observed_primary_reset_at_for_profile(path: Path) -> datetime | None:
    meta = read_profile_metadata(path)
    return parse_dt(meta.get("observed_primary_reset_at"))


def observed_account_id_for_profile(path: Path) -> str | None:
    meta = read_profile_metadata(path)
    value = meta.get("observed_account_id")
    return str(value) if value else None


def observed_secondary_used_percent_for_profile(path: Path) -> float | None:
    meta = read_profile_metadata(path)
    return _safe_float(meta.get("observed_secondary_used_percent"))


def observed_primary_used_percent_for_profile(path: Path) -> float | None:
    meta = read_profile_metadata(path)
    return _safe_float(meta.get("observed_primary_used_percent"))


def observed_limit_reached_for_profile(path: Path) -> bool | None:
    meta = read_profile_metadata(path)
    value = meta.get("observed_limit_reached")
    if isinstance(value, bool):
        return value
    return None


def observed_allowed_for_profile(path: Path) -> bool | None:
    meta = read_profile_metadata(path)
    value = meta.get("observed_allowed")
    if isinstance(value, bool):
        return value
    return None


def observed_block_until_for_profile(path: Path, *, now: datetime | None = None) -> tuple[datetime | None, str | None]:
    now = now or now_local()
    observed_secondary_used = observed_secondary_used_percent_for_profile(path)
    observed_secondary_reset = observed_secondary_reset_at_for_profile(path)
    if (
        observed_secondary_used is not None
        and observed_secondary_used >= 100
        and observed_secondary_reset is not None
        and observed_secondary_reset > now
    ):
        return observed_secondary_reset, "weekly_limit_unreset"

    observed_primary_used = observed_primary_used_percent_for_profile(path)
    observed_primary_reset = observed_primary_reset_at_for_profile(path)
    if (
        observed_primary_used is not None
        and observed_primary_used >= 100
        and observed_primary_reset is not None
        and observed_primary_reset > now
    ):
        return observed_primary_reset, "primary_5h_unreset"

    observed_limit_reached = observed_limit_reached_for_profile(path)
    observed_allowed = observed_allowed_for_profile(path)
    if observed_limit_reached is True or observed_allowed is False:
        if observed_secondary_reset is not None and observed_secondary_reset > now:
            return observed_secondary_reset, "weekly_limit_flagged"
        if observed_primary_reset is not None and observed_primary_reset > now:
            return observed_primary_reset, "primary_5h_flagged"

    return None, None


def effective_reset_at_for_profile(path: Path, summary: ProfileSummary) -> datetime:
    observed = observed_secondary_reset_at_for_profile(path)
    if observed is not None:
        return observed
    weekly = parse_dt(summary.weekly_reset_at)
    if weekly is not None:
        return weekly
    expired = parse_dt(summary.expired)
    if expired is not None:
        return expired
    return datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


def effective_reset_label_for_profile(path: Path, summary: ProfileSummary) -> tuple[str, str]:
    observed = observed_secondary_reset_at_for_profile(path)
    if observed is not None:
        return observed.isoformat(), "observed"
    if summary.weekly_reset_at:
        source = read_profile_metadata(path).get("weekly_reset_source") or "profile"
        return summary.weekly_reset_at, str(source)
    if summary.expired:
        return summary.expired, "expired"
    return "-", "unknown"


def usage_is_stale(path: Path, max_age_minutes: int) -> bool:
    checked_at = usage_checked_at_for_profile(path)
    if checked_at is None:
        return True
    age = now_local() - checked_at
    return age > timedelta(minutes=max_age_minutes)


def current_profile_usage_snapshot(
    source_dir: Path,
    managed_dir: Path,
    target_path: Path,
    *,
    max_age_minutes: int,
) -> tuple[RemoteUsageSnapshot | None, str | None]:
    current_account = current_auth_account_id(target_path)
    if current_account is None:
        return None, "current profile not found in pool"

    matches = [
        summarize_profile(path)
        for path in discover_all_profiles(source_dir, managed_dir)
        if summarize_profile(path).account_id == current_account
    ]
    if not matches:
        return None, "current profile not found in pool"

    matches.sort(
        key=lambda summary: (
            0 if not usage_is_stale(summary.path, max_age_minutes) else 1,
            -(usage_checked_at_for_profile(summary.path).timestamp() if usage_checked_at_for_profile(summary.path) else 0),
            source_rank(summary.source_kind),
            str(summary.path),
        )
    )
    current_summary = matches[0]
    path = current_summary.path

    observed_account_id = observed_account_id_for_profile(path)
    observed_email = str(read_profile_metadata(path).get("observed_email") or "") or None
    if observed_email and current_summary.email and observed_email != current_summary.email:
        return None, "current profile usage snapshot belongs to a different email"

    checked_at = usage_checked_at_for_profile(path)
    if checked_at is None:
        return None, "current profile usage snapshot is missing"
    if usage_is_stale(path, max_age_minutes):
        return None, "current profile usage snapshot is stale"

    return (
        RemoteUsageSnapshot(
            account_id=observed_account_id,
            email=observed_email or current_summary.email,
            plan_type=str(read_profile_metadata(path).get("observed_plan_type") or "") or None,
            allowed=observed_allowed_for_profile(path),
            limit_reached=observed_limit_reached_for_profile(path),
            primary_used_percent=observed_primary_used_percent_for_profile(path),
            primary_reset_at=observed_primary_reset_at_for_profile(path),
            primary_window_seconds=_safe_int(read_profile_metadata(path).get("observed_primary_window_seconds")),
            secondary_used_percent=observed_secondary_used_percent_for_profile(path),
            secondary_reset_at=observed_secondary_reset_at_for_profile(path),
            secondary_window_seconds=_safe_int(read_profile_metadata(path).get("observed_secondary_window_seconds")),
            fetched_at=checked_at or now_local(),
            source=str(read_profile_metadata(path).get("usage_source") or "profile_meta"),
        ),
        None,
    )


def fetch_remote_usage_for_payload(payload: dict[str, Any]) -> RemoteUsageSnapshot:
    normalized = normalize_source_payload(Path(str(payload.get("source_file", "<memory>"))), payload)
    access_token = str(normalized.get("access_token") or "")
    account_id = str(normalized.get("account_id") or "")
    if not access_token:
        raise SystemExit("missing access_token for remote usage fetch")

    request = urllib.request.Request(
        "https://chatgpt.com/backend-api/wham/usage",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "codex-auth-pool/0.1",
            **({"ChatGPT-Account-Id": account_id} if account_id else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"usage fetch failed with HTTP {exc.code}: {body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"usage fetch failed: {exc}") from exc

    data = json.loads(raw)
    rate_limit = data.get("rate_limit", {}) if isinstance(data, dict) else {}
    primary = rate_limit.get("primary_window", {}) if isinstance(rate_limit, dict) else {}
    secondary = rate_limit.get("secondary_window", {}) if isinstance(rate_limit, dict) else {}
    return RemoteUsageSnapshot(
        account_id=data.get("account_id") or data.get("user_id") or account_id,
        email=data.get("email") or normalized.get("email"),
        plan_type=data.get("plan_type"),
        allowed=rate_limit.get("allowed") if isinstance(rate_limit.get("allowed"), bool) else None,
        limit_reached=rate_limit.get("limit_reached") if isinstance(rate_limit.get("limit_reached"), bool) else None,
        primary_used_percent=_safe_float(primary.get("used_percent")),
        primary_reset_at=parse_unix_ts(primary.get("reset_at")),
        primary_window_seconds=_safe_int(primary.get("limit_window_seconds")),
        secondary_used_percent=_safe_float(secondary.get("used_percent")),
        secondary_reset_at=parse_unix_ts(secondary.get("reset_at")),
        secondary_window_seconds=_safe_int(secondary.get("limit_window_seconds")),
        fetched_at=now_local(),
        source="wham_usage",
    )


def refresh_profile_usage(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    snapshot = fetch_remote_usage_for_payload(payload)
    updates = {
        "observed_account_id": snapshot.account_id,
        "observed_email": snapshot.email,
        "observed_plan_type": snapshot.plan_type,
        "observed_allowed": snapshot.allowed,
        "observed_limit_reached": snapshot.limit_reached,
        "observed_primary_used_percent": snapshot.primary_used_percent,
        "observed_primary_reset_at": iso_or_none(snapshot.primary_reset_at),
        "observed_primary_window_seconds": snapshot.primary_window_seconds,
        "observed_secondary_used_percent": snapshot.secondary_used_percent,
        "observed_secondary_reset_at": iso_or_none(snapshot.secondary_reset_at),
        "observed_secondary_window_seconds": snapshot.secondary_window_seconds,
        "usage_checked_at": snapshot.fetched_at.isoformat(),
        "usage_source": snapshot.source,
    }
    return update_profile_metadata(path, **updates)


def iter_recent_session_files(sessions_dir: Path, limit: int = 30) -> list[Path]:
    if not sessions_dir.exists():
        return []
    files = [path for path in sessions_dir.rglob("*.jsonl") if path.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return files[:limit]


def latest_rate_limit_snapshot(sessions_dir: Path) -> RateLimitSnapshot | None:
    for path in iter_recent_session_files(sessions_dir):
        try:
            lines = path.read_text().splitlines()
        except UnicodeDecodeError:
            continue
        for raw in reversed(lines):
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload", {})
            if payload.get("type") != "token_count":
                continue
            rate_limits = payload.get("rate_limits", {})
            primary = rate_limits.get("primary", {})
            secondary = rate_limits.get("secondary", {})
            return RateLimitSnapshot(
                primary_used_percent=_safe_float(primary.get("used_percent")),
                primary_resets_at=parse_unix_ts(primary.get("resets_at")),
                primary_window_minutes=_safe_int(primary.get("window_minutes")),
                secondary_used_percent=_safe_float(secondary.get("used_percent")),
                secondary_resets_at=parse_unix_ts(secondary.get("resets_at")),
                secondary_window_minutes=_safe_int(secondary.get("window_minutes")),
                plan_type=rate_limits.get("plan_type"),
                source_file=path,
                event_timestamp=event.get("timestamp"),
            )
    return None


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def rank_profiles(
    source_dir: Path,
    managed_dir: Path,
    state: dict[str, Any],
    target_path: Path,
) -> list[dict[str, Any]]:
    now = now_local()
    current_account = current_auth_account_id(target_path)
    preferred_next = preferred_next_account_id(state)
    deduped: dict[str, dict[str, Any]] = {}
    for path in discover_all_profiles(source_dir, managed_dir):
        summary = summarize_profile(path)
        cd_until = cooldown_until(state, summary.account_id)
        limit_reset = limit_reset_at_for_profile(path)
        expired = parse_dt(summary.expired)
        weekly = effective_reset_at_for_profile(path, summary)
        remote_block_until, remote_block_reason = observed_block_until_for_profile(path, now=now)
        available = (
            not summary.disabled
            and (expired is None or expired > now)
            and (cd_until is None or cd_until <= now)
            and (limit_reset is None or limit_reset <= now)
            and (remote_block_until is None or remote_block_until <= now)
        )
        candidate = {
            "path": path,
            "summary": summary,
            "available": available,
            "cooldown_until": cd_until,
            "cooldown_reason": cooldown_reason(state, summary.account_id),
            "limit_reset_at": limit_reset,
            "limit_reason": limit_reason_for_profile(path),
            "remote_block_until": remote_block_until,
            "remote_block_reason": remote_block_reason,
            "is_current": summary.account_id == current_account,
            "is_preferred_next": summary.account_id == preferred_next,
            "weekly_sort": weekly,
        }
        existing = deduped.get(summary.account_id)
        if existing is None:
            deduped[summary.account_id] = candidate
            continue
        existing_summary: ProfileSummary = existing["summary"]
        better = (
            source_rank(summary.source_kind),
            0 if candidate["is_current"] else 1,
            parse_dt(summary.last_refresh) or datetime(1, 1, 1, tzinfo=timezone.utc),
            str(path),
        ) < (
            source_rank(existing_summary.source_kind),
            0 if existing["is_current"] else 1,
            parse_dt(existing_summary.last_refresh) or datetime(1, 1, 1, tzinfo=timezone.utc),
            str(existing["path"]),
        )
        if better:
            deduped[summary.account_id] = candidate

    ranked = list(deduped.values())
    ranked.sort(
        key=lambda item: (
            0 if item["available"] else 1,
            0 if item["is_preferred_next"] else 1,
            item["weekly_sort"],
            parse_dt(item["summary"].last_refresh) or datetime(1, 1, 1, tzinfo=timezone.utc),
            source_rank(item["summary"].source_kind),
            item["summary"].email,
        )
    )
    return ranked


def choose_best_profile(
    source_dir: Path,
    managed_dir: Path,
    state: dict[str, Any],
    target_path: Path,
) -> dict[str, Any] | None:
    ranked = rank_profiles(source_dir, managed_dir, state, target_path)
    for item in ranked:
        if item["available"]:
            return item
    return None


def cmd_list(args: argparse.Namespace) -> int:
    profiles = discover_all_profiles(args.source_dir, args.managed_dir)
    if not profiles:
        print(f"no auth profiles found in {args.source_dir} or {args.managed_dir}")
        return 0

    for index, path in enumerate(profiles, start=1):
        meta = summarize_profile(path)
        weekly = meta.weekly_reset_at or "-"
        last_refresh = meta.last_refresh or "-"
        expired = meta.expired or "-"
        disabled = "yes" if meta.disabled else "no"
        print(
            f"{index:02d}. {path.name}\n"
            f"    source_kind={meta.source_kind}\n"
            f"    email={meta.email or '-'}\n"
            f"    account_id={meta.account_id or '-'}\n"
            f"    weekly_reset_at={weekly}\n"
            f"    last_refresh={last_refresh}\n"
            f"    expired={expired}\n"
            f"    disabled={disabled}"
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = load_state(args.state_path)
    ranked = rank_profiles(args.source_dir, args.managed_dir, state, Path(args.target))
    if not ranked:
        print(f"no auth profiles found in {args.source_dir} or {args.managed_dir}")
        return 0

    snapshot = latest_rate_limit_snapshot(args.sessions_dir)
    current_summary = current_account_summary(Path(args.target), args.source_dir, args.managed_dir)
    current_item = next((item for item in ranked if item["is_current"]), None)
    next_item = next((item for item in ranked if item["available"] and not item["is_current"]), None)

    print("Current")
    print(f"  account: {current_summary.email if current_summary else '-'}")
    print(f"  profile: {current_summary.path.name if current_summary else '-'}")
    print(f"  account_id: {current_summary.account_id if current_summary else '-'}")
    print(f"  cooldown: {fmt_dt(current_item['cooldown_until']) if current_item else '-'}")
    print(f"  preferred_next: {'yes' if current_item and current_item['is_preferred_next'] else 'no'}")
    if current_summary:
        current_reset, current_reset_source = effective_reset_label_for_profile(current_summary.path, current_summary)
        print(f"  reset_at: {current_reset}")
        print(f"  reset_source: {current_reset_source}")
    else:
        print("  reset_at: -")
        print("  reset_source: -")
    print("")
    print("Limits")
    if snapshot is None:
        print("  latest snapshot: not found")
    else:
        print(f"  5h window: {fmt_percent(snapshot.primary_used_percent)} used, resets in {fmt_timedelta_until(snapshot.primary_resets_at)}")
        print(f"  weekly window: {fmt_percent(snapshot.secondary_used_percent)} used, resets in {fmt_timedelta_until(snapshot.secondary_resets_at)}")
        print(f"  snapshot time: {snapshot.event_timestamp or '-'}")
    print("")
    print("Next")
    if next_item is None:
        print("  next account: no alternate available account right now")
    else:
        summary = next_item["summary"]
        reset_label, reset_source = effective_reset_label_for_profile(summary.path, summary)
        print(f"  next account: {summary.email or summary.account_id}")
        print(f"  profile: {summary.path.name}")
        print(f"  reset_at: {reset_label}")
        print(f"  reset_source: {reset_source}")
    print("")
    print("Pool")
    for index, item in enumerate(ranked[: min(len(ranked), 8)], start=1):
        summary: ProfileSummary = item["summary"]
        flags = []
        if item["available"]:
            flags.append("available")
        else:
            flags.append("blocked")
        if item["is_current"]:
            flags.append("current")
        if summary.disabled:
            flags.append("source-disabled")
        if item["cooldown_until"] is not None and item["cooldown_until"] > now_local():
            flags.append(f"cooldown_until={item['cooldown_until'].isoformat()}")
        if item["cooldown_reason"]:
            flags.append(f"reason={item['cooldown_reason']}")
        if item["is_preferred_next"]:
            flags.append("preferred-next")
        if item["limit_reset_at"] is not None and item["limit_reset_at"] > now_local():
            flags.append(f"known_limit_reset={item['limit_reset_at'].isoformat()}")
        if item["remote_block_until"] is not None and item["remote_block_until"] > now_local():
            flags.append(f"remote_block_until={item['remote_block_until'].isoformat()}")
        if item.get("remote_block_reason"):
            flags.append(f"remote_reason={item['remote_block_reason']}")
        weekly, weekly_source = effective_reset_label_for_profile(summary.path, summary)
        print(f"  {index:02d}. {summary.email or summary.path.name}")
        print(f"      file: {summary.path.name}")
        print(f"      reset_at: {weekly}")
        print(f"      reset_source: {weekly_source}")
        print(f"      flags: {', '.join(flags)}")
    return 0


def cmd_pick(args: argparse.Namespace) -> int:
    state = load_state(args.state_path)
    picked = choose_best_profile(args.source_dir, args.managed_dir, state, Path(args.target))
    if picked is None:
        print("no currently available profile")
        return 1
    summary: ProfileSummary = picked["summary"]
    print(summary.path.name)
    print(f"source_kind={summary.source_kind}")
    print(f"email={summary.email}")
    print(f"account_id={summary.account_id}")
    print(f"weekly_reset_at={summary.weekly_reset_at or '-'}")
    return 0


def cmd_rate_limits(args: argparse.Namespace) -> int:
    snapshot = latest_rate_limit_snapshot(args.sessions_dir)
    if snapshot is None:
        print(f"no token_count rate limit snapshot found in {args.sessions_dir}")
        return 1
    print("Rate Limits")
    print(f"  plan: {snapshot.plan_type or '-'}")
    print(f"  5h window: {fmt_percent(snapshot.primary_used_percent)} used")
    print(f"  5h reset: {fmt_dt(snapshot.primary_resets_at)}")
    print(f"  weekly window: {fmt_percent(snapshot.secondary_used_percent)} used")
    print(f"  weekly reset: {fmt_dt(snapshot.secondary_resets_at)}")
    print(f"  snapshot time: {snapshot.event_timestamp or '-'}")
    print(f"  source file: {snapshot.source_file}")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    profile_path = pick_profile(args.source_dir, args.managed_dir, args.profile)
    payload = convert_profile(normalize_source_payload(profile_path, read_json(profile_path)))
    print(json.dumps(redacted_payload(payload), ensure_ascii=False, indent=2))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    profile_path = pick_profile(args.source_dir, args.managed_dir, args.profile)
    payload = convert_profile(normalize_source_payload(profile_path, read_json(profile_path)))
    output_path = Path(args.output).expanduser() if args.output else export_path_for(
        profile_path, args.export_dir
    )
    write_json(output_path, payload)
    print(f"exported converted Codex auth to {output_path}")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    profile_path = pick_profile(args.source_dir, args.managed_dir, args.profile)
    normalized = normalize_source_payload(profile_path, read_json(profile_path))
    payload = convert_profile(normalized)
    target_path = Path(args.target).expanduser()

    if target_path.exists():
        backup = make_backup(target_path)
        print(f"backed up existing Codex auth to {backup}")
    else:
        target_path.parent.mkdir(parents=True, exist_ok=True)

    write_json(target_path, payload)
    synced_targets = sync_secondary_auth_files(payload, target_path)
    if getattr(args, "state_path", None):
        state = load_state(args.state_path)
        state["current_profile"] = str(profile_path)
        if state.get("preferred_next_account_id") == normalized.get("account_id"):
            state.pop("preferred_next_account_id", None)
        save_state(args.state_path, state)
    print(f"applied profile {profile_path.name} to {target_path}")
    for synced in synced_targets:
        print(f"synced secondary auth file {synced}")
    if getattr(args, "restart_after_switch", False):
        restart_codex_app(Path(args.app_path), hard=not getattr(args, "graceful_restart", False))
        print(f"restarted Codex app {args.app_path}")
    append_event(
        args.events_path,
        "apply_profile",
        profile=str(profile_path),
        source_kind=normalized.get("source_kind"),
        email=normalized.get("email"),
        account_id=normalized.get("account_id"),
        restart_after_switch=bool(getattr(args, "restart_after_switch", False)),
    )
    send_notification(
        "Codex Auth Pool",
        f"Switched to {normalized.get('email') or normalized.get('account_id')}",
        enabled=not getattr(args, "no_notify", False),
    )
    if not getattr(args, "restart_after_switch", False):
        print("next step: fully quit and reopen Codex Desktop before testing")
    return 0


def cmd_apply_best(args: argparse.Namespace) -> int:
    state = load_state(args.state_path)
    picked = choose_best_profile(args.source_dir, args.managed_dir, state, Path(args.target))
    if picked is None:
        print("no currently available profile")
        return 1
    args.profile = str(picked["path"])
    return cmd_apply(args)


def cmd_cooldown(args: argparse.Namespace) -> int:
    profile_path = pick_profile(args.source_dir, args.managed_dir, args.profile)
    summary = summarize_profile(profile_path)
    state = load_state(args.state_path)
    record = profile_record(state, summary.account_id)

    if args.clear:
        record.pop("cooldown_until", None)
        record.pop("cooldown_reason", None)
        save_state(args.state_path, state)
        print(f"cleared cooldown for {summary.email}")
        return 0

    hours = args.hours
    until = now_local() + timedelta(hours=hours)
    record["cooldown_until"] = until.isoformat()
    record["cooldown_reason"] = args.reason or "manual_cooldown"
    save_state(args.state_path, state)
    append_event(
        args.events_path,
        "manual_cooldown",
        profile=str(profile_path),
        email=summary.email,
        account_id=summary.account_id,
        cooldown_until=until.isoformat(),
        reason=record["cooldown_reason"],
    )
    print(
        f"set cooldown for {summary.email} until {until.isoformat()} "
        f"(reason={record['cooldown_reason']})"
    )
    return 0


def current_auth_payload(target: Path, root_target: Path) -> tuple[Path, dict[str, Any]]:
    primary = target if target.exists() else root_target
    if not primary.exists():
        raise SystemExit(f"no current Codex auth found at {target} or {root_target}")
    return primary, read_json(primary)


def save_managed_profile_from_payload(
    payload: dict[str, Any],
    destination_dir: Path,
    name: str | None,
    source_kind: str,
    source_file: str | None = None,
) -> Path:
    primary_path = Path(source_file) if source_file else Path("<memory>")
    normalized = normalize_source_payload(primary_path, payload, read_profile_metadata(primary_path))
    normalized["source_kind"] = source_kind
    if source_file:
        normalized["source_file"] = source_file
    out = destination_dir / managed_profile_name(normalized, explicit_name=name)
    write_json(out, convert_profile(normalized))
    write_json(meta_path_for_profile(out), metadata_for_profile(normalized, out))
    return out


def migrate_managed_profiles(managed_dir: Path) -> list[Path]:
    migrated: list[Path] = []
    for path in discover_managed_profiles(managed_dir):
        payload = read_json(path)
        if "tokens" in payload:
            if not meta_path_for_profile(path).exists():
                normalized = normalize_source_payload(path, payload)
                write_json(meta_path_for_profile(path), metadata_for_profile(normalized, path))
                migrated.append(path)
            continue

        backup = make_backup(path)
        normalized = normalize_source_payload(path, payload)
        write_json(path, convert_profile(normalized))
        write_json(meta_path_for_profile(path), metadata_for_profile(normalized, path))
        append_event(DEFAULT_EVENTS_PATH, "migrate_managed_profile", profile=str(path), backup=str(backup))
        migrated.append(path)
    return migrated


def sync_cliproxy_into_managed(source_dir: Path, managed_dir: Path) -> list[Path]:
    synced: list[Path] = []
    existing_accounts = {
        summarize_profile(path).account_id: path
        for path in discover_managed_profiles(managed_dir)
    }
    for source_path in discover_profiles(source_dir):
        normalized = normalize_source_payload(source_path, read_json(source_path))
        if normalized.get("account_id") in existing_accounts:
            continue
        out = save_managed_profile_from_payload(
            normalized,
            managed_dir,
            name=source_path.stem,
            source_kind="managed",
            source_file=str(source_path),
        )
        synced.append(out)
        existing_accounts[normalized["account_id"]] = out
    return synced


def cmd_save_current(args: argparse.Namespace) -> int:
    source_path, payload = current_auth_payload(Path(args.target), DEFAULT_CODEX_ROOT_AUTH_PATH)
    out = save_managed_profile_from_payload(
        payload,
        args.managed_dir,
        args.name,
        source_kind="managed",
        source_file=str(source_path),
    )
    append_event(
        args.events_path,
        "save_current",
        source_path=str(source_path),
        output_path=str(out),
    )
    send_notification(
        "Codex Auth Pool",
        f"Saved current login as {out.stem}",
        enabled=not getattr(args, "no_notify", False),
    )
    print(f"saved current Codex login into managed vault: {out}")
    return 0


def cmd_import_auth_file(args: argparse.Namespace) -> int:
    auth_path = Path(args.auth_file).expanduser()
    payload = read_json(auth_path)
    out = save_managed_profile_from_payload(
        payload,
        args.managed_dir,
        args.name,
        source_kind=args.source_kind or "manual",
        source_file=str(auth_path),
    )
    append_event(
        args.events_path,
        "import_auth_file",
        auth_file=str(auth_path),
        output_path=str(out),
        source_kind=args.source_kind or "manual",
    )
    send_notification(
        "Codex Auth Pool",
        f"Imported auth as {out.stem}",
        enabled=not getattr(args, "no_notify", False),
    )
    print(f"imported auth file into managed vault: {out}")
    return 0


def cmd_migrate_managed(args: argparse.Namespace) -> int:
    migrated = migrate_managed_profiles(args.managed_dir)
    print(f"migrated_managed_profiles={len(migrated)}")
    for path in migrated:
        print(path)
    return 0


def cmd_sync_cliproxy(args: argparse.Namespace) -> int:
    before_accounts = {
        summarize_profile(path).account_id
        for path in discover_managed_profiles(args.managed_dir)
    }
    synced = sync_cliproxy_into_managed(args.source_dir, args.managed_dir)
    source_count = len(discover_profiles(args.source_dir))
    skipped = max(source_count - len(synced), 0)
    append_event(
        args.events_path,
        "sync_cliproxy_into_managed",
        synced_count=len(synced),
    )
    print("Sync Result")
    print(f"  source profiles found: {source_count}")
    print(f"  imported into managed vault: {len(synced)}")
    print(f"  skipped: {skipped}")
    if skipped:
        print("  skip reason: same account_id already existed in managed vault")
    if synced:
        print("")
        print("Imported")
        for path in synced:
            summary = summarize_profile(path)
            status = "new" if summary.account_id not in before_accounts else "updated"
            print(f"  - {summary.email or summary.account_id} ({path.name}, {status})")
    else:
        print("  nothing new to import")
    return 0


def cmd_set_weekly_reset(args: argparse.Namespace) -> int:
    profile_path = pick_profile(args.source_dir, args.managed_dir, args.profile)
    summary = summarize_profile(profile_path)
    if args.clear:
        meta = update_profile_metadata(profile_path, weekly_reset_at=None, weekly_reset_source="manual_cleared")
        print(f"cleared weekly_reset_at for {summary.email or profile_path.name}")
    else:
        value = args.when
        parsed = parse_dt(value)
        if parsed is None:
            raise SystemExit("invalid datetime format; use ISO 8601 like 2026-04-19T03:25:32+08:00")
        meta = update_profile_metadata(profile_path, weekly_reset_at=parsed.isoformat(), weekly_reset_source="manual")
        print(f"set weekly_reset_at for {summary.email or profile_path.name} -> {parsed.isoformat()}")
    append_event(
        args.events_path,
        "set_weekly_reset",
        profile=str(profile_path),
        email=summary.email,
        account_id=summary.account_id,
        weekly_reset_at=meta.get("weekly_reset_at"),
    )
    return 0


def cmd_prefer_next(args: argparse.Namespace) -> int:
    state = load_state(args.state_path)
    if args.clear:
        state.pop("preferred_next_account_id", None)
        save_state(args.state_path, state)
        append_event(args.events_path, "clear_preferred_next")
        print("cleared preferred next account")
        return 0

    profile_path = pick_profile(args.source_dir, args.managed_dir, args.profile)
    summary = summarize_profile(profile_path)
    state["preferred_next_account_id"] = summary.account_id
    save_state(args.state_path, state)
    append_event(
        args.events_path,
        "set_preferred_next",
        profile=str(profile_path),
        email=summary.email,
        account_id=summary.account_id,
    )
    print(f"preferred next account set to {summary.email or summary.account_id}")
    print(f"profile={profile_path.name}")
    return 0


def cmd_refresh_usage(args: argparse.Namespace) -> int:
    if getattr(args, "profile", None):
        profiles = [pick_profile(args.source_dir, args.managed_dir, args.profile)]
    else:
        deduped: dict[str, Path] = {}
        for path in discover_all_profiles(args.source_dir, args.managed_dir):
            summary = summarize_profile(path)
            existing = deduped.get(summary.account_id)
            if existing is None:
                deduped[summary.account_id] = path
                continue
            existing_summary = summarize_profile(existing)
            if source_rank(summary.source_kind) < source_rank(existing_summary.source_kind):
                deduped[summary.account_id] = path
        profiles = list(deduped.values())
    if not profiles:
        print(f"no auth profiles found in {args.source_dir} or {args.managed_dir}")
        return 0

    refreshed = 0
    failed = 0
    print("Usage Refresh")
    for path in profiles:
        if not getattr(args, "force", False) and usage_is_stale(path, args.max_age_minutes) is False:
            checked_at = usage_checked_at_for_profile(path)
            print(f"  - {path.name}: skipped (fresh until {checked_at.isoformat() if checked_at else '-'})")
            continue
        try:
            meta = refresh_profile_usage(path)
            refreshed += 1
            print(
                f"  - {path.name}: weekly={meta.get('observed_secondary_used_percent', '-')}% "
                f"reset={meta.get('observed_secondary_reset_at', '-')}"
            )
        except SystemExit as exc:
            failed += 1
            print(f"  - {path.name}: failed ({exc})")

    append_event(
        args.events_path,
        "refresh_usage",
        refreshed_count=refreshed,
        failed_count=failed,
        profile=getattr(args, "profile", None),
        force=bool(getattr(args, "force", False)),
    )
    print("")
    print(f"summary: refreshed={refreshed} failed={failed}")
    return 0 if failed == 0 else 1


def detect_paths(
    *,
    target: Path | None = None,
    source_dir: Path | None = None,
    managed_dir: Path | None = None,
    sessions_dir: Path | None = None,
    app_path: Path | None = None,
) -> dict[str, Any]:
    codex_cache_auth = target or DEFAULT_CODEX_AUTH_PATH
    codex_root_auth = DEFAULT_CODEX_ROOT_AUTH_PATH
    cliproxy_dir = source_dir or DEFAULT_CLIPROXY_DIR
    managed_dir = managed_dir or DEFAULT_MANAGED_DIR
    sessions_dir = sessions_dir or DEFAULT_CODEX_SESSIONS_DIR
    app_path = app_path or DEFAULT_CODEX_APP

    return {
        "codex_cache_auth": {
            "path": str(codex_cache_auth),
            "exists": codex_cache_auth.exists(),
        },
        "codex_root_auth": {
            "path": str(codex_root_auth),
            "exists": codex_root_auth.exists(),
        },
        "cliproxy_dir": {
            "path": str(cliproxy_dir),
            "exists": cliproxy_dir.exists(),
            "profile_count": len(discover_profiles(cliproxy_dir)) if cliproxy_dir.exists() else 0,
        },
        "managed_dir": {
            "path": str(managed_dir),
            "exists": managed_dir.exists(),
            "profile_count": len(discover_managed_profiles(managed_dir)) if managed_dir.exists() else 0,
        },
        "sessions_dir": {
            "path": str(sessions_dir),
            "exists": sessions_dir.exists(),
        },
        "codex_app": {
            "path": str(app_path),
            "exists": app_path.exists(),
        },
    }


def cmd_info(args: argparse.Namespace) -> int:
    command_path = shutil_lib.which("codex-auth-pool")
    payload = {
        "tool": {
            "package_entry": str(Path(__file__).resolve()),
            "python_executable": sys.executable,
            "command_path": command_path,
            "current_working_directory": str(Path.cwd()),
        },
        "runtime_state": {
            "codex_cache_auth": str(DEFAULT_CODEX_AUTH_PATH),
            "codex_root_auth": str(DEFAULT_CODEX_ROOT_AUTH_PATH),
            "codex_home": str(DEFAULT_CODEX_AUTH_PATH.parent.parent),
            "auth_pool_home": str(DEFAULT_CONFIG_PATH.parent),
            "cliproxy_home": str(DEFAULT_CLIPROXY_DIR),
        },
        "note": (
            "The source checkout can live anywhere. Actual Codex account, plugin, "
            "connector, and auth-pool state is stored under your home directory."
        ),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    issues = 0
    warnings = 0

    def report(level: str, label: str, detail: str) -> None:
        print(f"[{level}] {label}: {detail}")

    cache_auth = DEFAULT_CODEX_AUTH_PATH
    root_auth = DEFAULT_CODEX_ROOT_AUTH_PATH
    cache_exists = cache_auth.exists()
    root_exists = root_auth.exists()
    if cache_exists:
        report("OK", "cache auth", str(cache_auth))
    else:
        issues += 1
        report("ERR", "cache auth", f"missing at {cache_auth}")

    if root_exists:
        report("OK", "root auth", str(root_auth))
    else:
        issues += 1
        report("ERR", "root auth", f"missing at {root_auth}")

    cache_account = current_auth_account_id(cache_auth)
    root_account = current_auth_account_id(root_auth)
    if cache_exists and root_exists:
        if cache_account and root_account and cache_account == root_account:
            report("OK", "auth sync", f"account_id={cache_account}")
        else:
            issues += 1
            report(
                "ERR",
                "auth sync",
                f"cache account={cache_account or '-'} root account={root_account or '-'}",
            )

    profile_count = len(discover_all_profiles(args.source_dir, args.managed_dir))
    if profile_count > 0:
        report("OK", "profile pool", f"{profile_count} profiles discovered")
    else:
        warnings += 1
        report("WARN", "profile pool", "no profiles discovered yet")

    snapshots = list_env_snapshots(args.env_snapshots_dir)
    if snapshots:
        report("OK", "environment snapshot", f"latest={snapshots[0].name}")
    else:
        warnings += 1
        report("WARN", "environment snapshot", "no snapshot found; run `codex-auth-pool snapshot-env --name baseline`")

    if args.config_path.exists():
        report("OK", "config", str(args.config_path))
    else:
        warnings += 1
        report("WARN", "config", f"missing at {args.config_path}; run `codex-auth-pool init`")

    if Path(args.app_path).exists():
        report("OK", "Codex app", str(args.app_path))
    else:
        issues += 1
        report("ERR", "Codex app", f"missing at {args.app_path}")

    launchd = launchctl_status(DEFAULT_LAUNCHD_LABEL)
    if launchd["installed"] and launchd["loaded"]:
        report("OK", "launchd", f"active label={DEFAULT_LAUNCHD_LABEL}")
    elif launchd["installed"]:
        warnings += 1
        report("WARN", "launchd", "plist exists but agent is not loaded")
    else:
        warnings += 1
        report("WARN", "launchd", "not installed")

    snapshot = latest_rate_limit_snapshot(args.sessions_dir)
    current_summary = current_account_summary(Path(args.target), args.source_dir, args.managed_dir)
    ranked = rank_profiles(args.source_dir, args.managed_dir, load_state(args.state_path), Path(args.target))
    next_profile = next((item for item in ranked if item["available"] and not item["is_current"]), None)
    recent_event = read_recent_event(args.events_path)
    preferred_next = preferred_next_account_id(load_state(args.state_path))

    print("\nOverview")
    print(f"  current account: {current_summary.email if current_summary else '-'}")
    if snapshot is not None:
        print(f"  5h window: {fmt_percent(snapshot.primary_used_percent)} used, resets {fmt_dt(snapshot.primary_resets_at)}")
        print(f"  weekly window: {fmt_percent(snapshot.secondary_used_percent)} used, resets {fmt_dt(snapshot.secondary_resets_at)}")
    else:
        print("  latest limits: not found")
    if next_profile is not None:
        print(f"  next candidate: {next_profile['summary'].email or next_profile['summary'].account_id}")
        reset_label, reset_source = effective_reset_label_for_profile(next_profile["summary"].path, next_profile["summary"])
        print(f"  next reset: {reset_label} ({reset_source})")
    else:
        print("  next candidate: none")
    print(f"  preferred next: {preferred_next or '-'}")
    if recent_event is not None:
        print(f"  recent event: {recent_event.get('event_type', '-')} at {recent_event.get('timestamp', '-')}")

    state = "healthy" if issues == 0 else "needs_attention"
    print(f"\nsummary: {state} ({issues} errors, {warnings} warnings)")
    return 0 if issues == 0 else 1


def cmd_dashboard(args: argparse.Namespace) -> int:
    state = load_state(args.state_path)
    target_path = Path(args.target)
    current_summary = current_account_summary(target_path, args.source_dir, args.managed_dir)
    ranked = rank_profiles(args.source_dir, args.managed_dir, state, target_path)
    current_item = next((item for item in ranked if item["is_current"]), None)
    next_item = next((item for item in ranked if item["available"] and not item["is_current"]), None)
    snapshot = latest_rate_limit_snapshot(args.sessions_dir)
    launchd = launchctl_status(DEFAULT_LAUNCHD_LABEL)
    recent_event = read_recent_event(args.events_path)

    print("Codex Auth Pool Dashboard")
    print("")
    print("Now")
    print(f"  account: {current_summary.email if current_summary else '-'}")
    print(f"  profile: {current_summary.path.name if current_summary else '-'}")
    print(f"  account_id: {current_summary.account_id if current_summary else '-'}")
    print(f"  cooldown_until: {fmt_dt(current_item['cooldown_until']) if current_item else '-'}")
    print("")
    print("Limits")
    if snapshot is None:
        print("  5h window: -")
        print("  weekly window: -")
        print("  last snapshot: not found")
    else:
        print(f"  5h window: {fmt_percent(snapshot.primary_used_percent)} used, resets in {fmt_timedelta_until(snapshot.primary_resets_at)}")
        print(f"  weekly window: {fmt_percent(snapshot.secondary_used_percent)} used, resets in {fmt_timedelta_until(snapshot.secondary_resets_at)}")
        print(f"  last snapshot: {snapshot.event_timestamp or '-'}")
    print("")
    print("Rotation")
    print(f"  candidates available: {sum(1 for item in ranked if item['available'])}")
    print(f"  preferred next account_id: {preferred_next_account_id(state) or '-'}")
    if next_item is None:
        print("  next account: no alternate available account right now")
    else:
        summary = next_item["summary"]
        reset_label, reset_source = effective_reset_label_for_profile(summary.path, summary)
        print(f"  next account: {summary.email or summary.account_id}")
        print(f"  next profile: {summary.path.name}")
        print(f"  next reset: {reset_label}")
        print(f"  next reset source: {reset_source}")
        print(f"  next preferred: {'yes' if next_item['is_preferred_next'] else 'no'}")
    print("")
    print("Daemon")
    print(f"  installed: {'yes' if launchd['installed'] else 'no'}")
    print(f"  loaded: {'yes' if launchd['loaded'] else 'no'}")
    print(f"  state: {launchd['state'] or '-'}")
    print(f"  pid: {launchd['pid'] or '-'}")
    stdout_line = read_recent_log_line(DEFAULT_LAUNCHD_STDOUT)
    stderr_line = read_recent_log_line(DEFAULT_LAUNCHD_STDERR)
    stdout_time = file_mtime(DEFAULT_LAUNCHD_STDOUT)
    stderr_time = file_mtime(DEFAULT_LAUNCHD_STDERR)
    print(f"  recent stdout: {stdout_line or '-'}")
    print(f"  stdout time: {fmt_dt(stdout_time)}")
    print(f"  recent stderr: {stderr_line or '-'}")
    print(f"  stderr time: {fmt_dt(stderr_time)}")
    print("")
    print("Recent Event")
    if recent_event is None:
        print("  none")
    else:
        print(f"  type: {recent_event.get('event_type', '-')}")
        print(f"  time: {recent_event.get('timestamp', '-')}")
    print("")
    print("Top Pool")
    for index, item in enumerate(ranked[: min(len(ranked), 5)], start=1):
        summary = item["summary"]
        role = "current" if item["is_current"] else ("next" if next_item and item["summary"].account_id == next_item["summary"].account_id else "")
        suffix = f" [{role}]" if role else ""
        reset_label, reset_source = effective_reset_label_for_profile(summary.path, summary)
        print(f"  {index:02d}. {summary.email or summary.account_id}{suffix}")
        print(f"      profile: {summary.path.name}")
        print(f"      reset_at: {reset_label}")
        print(f"      reset_source: {reset_source}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    report = detect_paths(
        target=Path(args.target),
        source_dir=args.source_dir,
        managed_dir=args.managed_dir,
        sessions_dir=args.sessions_dir,
        app_path=Path(args.app_path),
    )
    report["config_path"] = {
        "path": str(args.config_path),
        "exists": args.config_path.exists(),
    }
    snapshots = list_env_snapshots(args.env_snapshots_dir)
    report["env_snapshots_dir"] = {
        "path": str(args.env_snapshots_dir),
        "exists": args.env_snapshots_dir.exists(),
        "snapshot_count": len(snapshots),
        "latest_snapshot": snapshots[0].name if snapshots else None,
    }
    report["launchd"] = launchctl_status(DEFAULT_LAUNCHD_LABEL)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_config_init(args: argparse.Namespace) -> int:
    detected = detect_paths(
        target=Path(args.target),
        source_dir=args.source_dir,
        managed_dir=args.managed_dir,
        sessions_dir=args.sessions_dir,
        app_path=Path(args.app_path),
    )
    payload = {
        "source_dir": detected["cliproxy_dir"]["path"],
        "managed_dir": detected["managed_dir"]["path"],
        "target": detected["codex_cache_auth"]["path"],
        "sessions_dir": detected["sessions_dir"]["path"],
        "app_path": detected["codex_app"]["path"],
        "state_path": str(args.state_path),
        "events_path": str(args.events_path),
        "env_snapshots_dir": str(args.env_snapshots_dir),
    }
    save_config(args.config_path, payload)
    print(f"wrote config to {args.config_path}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    cmd_config_init(args)
    if args.snapshot_env:
        snapshot_args = argparse.Namespace(
            env_snapshots_dir=args.env_snapshots_dir,
            name=args.snapshot_name,
            events_path=args.events_path,
            no_notify=args.no_notify,
        )
        cmd_snapshot_env(snapshot_args)
    if args.install_launchd:
        args.label = DEFAULT_LAUNCHD_LABEL
        args.stdout_path = DEFAULT_LAUNCHD_STDOUT
        args.stderr_path = DEFAULT_LAUNCHD_STDERR
        args.interval_seconds = args.interval_seconds
        args.primary_threshold = args.primary_threshold
        args.secondary_threshold = args.secondary_threshold
        args.restart_after_switch = args.restart_after_switch
        cmd_launchd_install(args)
    print("setup complete")
    print("recommended next steps:")
    print("  1. codex-auth-pool doctor")
    print("  2. codex-auth-pool save-current --name my-official-1")
    print("  3. codex-auth-pool status")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    print("initializing codex-auth-pool...")
    cmd_config_init(args)

    migrate_args = argparse.Namespace(managed_dir=args.managed_dir)
    cmd_migrate_managed(migrate_args)

    if args.snapshot_env:
        snapshot_args = argparse.Namespace(
            env_snapshots_dir=args.env_snapshots_dir,
            name=args.snapshot_name,
            events_path=args.events_path,
            no_notify=args.no_notify,
        )
        cmd_snapshot_env(snapshot_args)

    if args.save_current:
        try:
            save_args = argparse.Namespace(
                target=args.target,
                managed_dir=args.managed_dir,
                name=args.profile_name,
                events_path=args.events_path,
                no_notify=args.no_notify,
            )
            cmd_save_current(save_args)
        except SystemExit as exc:
            print(f"skipped save-current: {exc}")

    if args.sync_cliproxy:
        sync_args = argparse.Namespace(
            source_dir=args.source_dir,
            managed_dir=args.managed_dir,
            events_path=args.events_path,
        )
        cmd_sync_cliproxy(sync_args)

    if args.install_launchd:
        launchd_args = argparse.Namespace(**vars(args))
        launchd_args.label = DEFAULT_LAUNCHD_LABEL
        launchd_args.stdout_path = DEFAULT_LAUNCHD_STDOUT
        launchd_args.stderr_path = DEFAULT_LAUNCHD_STDERR
        cmd_launchd_install(launchd_args)

    print("init complete")
    print("you can now use:")
    print("  codex-auth-pool status")
    print("  codex-auth-pool pick")
    print("  codex-auth-pool apply-best --restart-after-switch")
    if args.install_launchd:
        print("  codex-auth-pool launchd-status")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    if not args.events_path.exists():
        print(f"no events file found at {args.events_path}")
        return 0
    lines = args.events_path.read_text(encoding="utf-8").splitlines()
    tail = lines[-args.limit :] if args.limit > 0 else lines
    for line in tail:
        print(line)
    return 0


def cmd_env_status(args: argparse.Namespace) -> int:
    snapshots = list_env_snapshots(args.env_snapshots_dir)
    tracked = []
    for item_name, source_path in env_snapshot_items():
        tracked.append(
            {
                "name": item_name,
                "path": str(source_path),
                "exists": source_path.exists(),
                "size": format_bytes(dir_size(source_path)),
            }
        )

    report = {
        "env_snapshots_dir": str(args.env_snapshots_dir),
        "tracked_items": tracked,
        "snapshot_count": len(snapshots),
        "snapshots": [path.name for path in snapshots[:10]],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_snapshot_env(args: argparse.Namespace) -> int:
    snapshot_dir, manifest = create_env_snapshot(
        args.env_snapshots_dir,
        name=args.name,
        note="manual environment snapshot",
    )
    total_size = sum(int(item.get("size_bytes") or 0) for item in manifest.get("items", []))
    append_event(
        args.events_path,
        "snapshot_env",
        snapshot_dir=str(snapshot_dir),
        item_count=len(manifest.get("items", [])),
        total_size_bytes=total_size,
    )
    send_notification(
        "Codex Auth Pool",
        f"Environment snapshot saved: {snapshot_dir.name}",
        enabled=not getattr(args, "no_notify", False),
    )
    print(f"saved environment snapshot to {snapshot_dir}")
    print(f"tracked_items={len(manifest.get('items', []))}")
    print(f"total_size={format_bytes(total_size)}")
    return 0


def cmd_restore_env(args: argparse.Namespace) -> int:
    snapshots = list_env_snapshots(args.env_snapshots_dir)
    if not snapshots:
        raise SystemExit(f"no snapshots found in {args.env_snapshots_dir}")
    snapshot_dir = args.snapshot_dir
    if snapshot_dir is None:
        snapshot_dir = snapshots[0]
    else:
        candidate = Path(str(snapshot_dir)).expanduser()
        snapshot_dir = candidate if candidate.exists() else args.env_snapshots_dir / str(snapshot_dir)
    if not snapshot_dir.exists():
        raise SystemExit(f"snapshot not found: {snapshot_dir}")

    backup_dir, _ = create_env_snapshot(
        args.env_snapshots_dir,
        name=f"backup-before-restore-{datetime.now().strftime(BACKUP_TIMESTAMP_FMT)}",
        note=f"automatic backup before restoring {snapshot_dir.name}",
    )
    restored = restore_env_snapshot(snapshot_dir)
    append_event(
        args.events_path,
        "restore_env",
        snapshot_dir=str(snapshot_dir),
        backup_dir=str(backup_dir),
        restored_count=len(restored["restored_items"]),
    )
    send_notification(
        "Codex Auth Pool",
        f"Environment restored from {snapshot_dir.name}",
        enabled=not getattr(args, "no_notify", False),
    )
    print(f"restored environment from {snapshot_dir}")
    print(f"automatic backup saved to {backup_dir}")
    print(f"restored_items={len(restored['restored_items'])}")
    if getattr(args, "restart_codex", False):
        restart_codex_app(Path(args.app_path), hard=not getattr(args, "graceful_restart", False))
        print(f"restarted Codex app {args.app_path}")
    else:
        print("next step: restart Codex Desktop if you want connector/plugin state to reload immediately")
    return 0


def set_cooldown_by_account_id(
    state_path: Path,
    account_id: str,
    until: datetime,
    reason: str,
) -> None:
    state = load_state(state_path)
    record = profile_record(state, account_id)
    record["cooldown_until"] = until.isoformat()
    record["cooldown_reason"] = reason
    save_state(state_path, state)


def cmd_tick(args: argparse.Namespace) -> int:
    if getattr(args, "refresh_usage", False):
        refresh_args = argparse.Namespace(
            source_dir=args.source_dir,
            managed_dir=args.managed_dir,
            profile=None,
            force=False,
            max_age_minutes=args.usage_max_age_minutes,
            events_path=args.events_path,
        )
        cmd_refresh_usage(refresh_args)

    current_account = current_auth_account_id(Path(args.target))
    if current_account is None:
        print("no current Codex auth account detected")
        return 1

    state = load_state(args.state_path)
    snapshot = latest_rate_limit_snapshot(args.sessions_dir)
    current_usage, usage_note = current_profile_usage_snapshot(
        args.source_dir,
        args.managed_dir,
        Path(args.target),
        max_age_minutes=args.usage_max_age_minutes,
    )

    trigger_primary_used = None
    trigger_primary_reset = None
    trigger_secondary_used = None
    trigger_secondary_reset = None
    trigger_source = None

    if current_usage is not None:
        trigger_primary_used = current_usage.primary_used_percent
        trigger_primary_reset = current_usage.primary_reset_at
        trigger_secondary_used = current_usage.secondary_used_percent
        trigger_secondary_reset = current_usage.secondary_reset_at
        trigger_source = f"profile_usage:{current_usage.source}"
    elif snapshot is not None and not getattr(args, "refresh_usage", False):
        trigger_primary_used = snapshot.primary_used_percent
        trigger_primary_reset = snapshot.primary_resets_at
        trigger_secondary_used = snapshot.secondary_used_percent
        trigger_secondary_reset = snapshot.secondary_resets_at
        trigger_source = f"session_snapshot:{snapshot.source_file.name}"
    else:
        note = usage_note or "no current-account usage snapshot available"
        print(f"no safe rotation signal; skipping auto-rotation ({note})")
        return 0

    triggered_reason = None
    triggered_until = None

    if (
        trigger_secondary_used is not None
        and trigger_secondary_used >= args.secondary_threshold
        and trigger_secondary_reset is not None
    ):
        triggered_reason = "weekly_limit"
        triggered_until = trigger_secondary_reset
    elif (
        trigger_primary_used is not None
        and trigger_primary_used >= args.primary_threshold
        and trigger_primary_reset is not None
    ):
        triggered_reason = "primary_5h_limit"
        triggered_until = trigger_primary_reset

    if triggered_reason and triggered_until:
        set_cooldown_by_account_id(args.state_path, current_account, triggered_until, triggered_reason)
        append_event(
            args.events_path,
            "auto_cooldown",
            account_id=current_account,
            cooldown_until=triggered_until.isoformat(),
            reason=triggered_reason,
            primary_used_percent=trigger_primary_used,
            secondary_used_percent=trigger_secondary_used,
            trigger_source=trigger_source,
        )
        print(
            f"marked current account {current_account} on cooldown until "
            f"{triggered_until.isoformat()} ({triggered_reason}, source={trigger_source})"
        )
        if not args.no_apply_best:
            picked = choose_best_profile(args.source_dir, args.managed_dir, load_state(args.state_path), Path(args.target))
            if picked and picked["summary"].account_id != current_account:
                args.profile = str(picked["path"])
                cmd_apply(args)
            else:
                append_event(
                    args.events_path,
                    "rotation_blocked",
                    account_id=current_account,
                    reason=triggered_reason,
                )
                print("no alternate available profile to switch to")
        return 0

    print(f"no rotation trigger; current account remains active (source={trigger_source})")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    while True:
        try:
            cmd_tick(args)
        except Exception as exc:  # pragma: no cover
            print(f"tick failed: {exc}", file=sys.stderr)
        time.sleep(args.interval_seconds)


def cmd_restart_codex(args: argparse.Namespace) -> int:
    restart_codex_app(Path(args.app_path), hard=not args.graceful_restart)
    mode = "graceful" if args.graceful_restart else "hard"
    print(f"restarted Codex app via {mode} restart: {args.app_path}")
    return 0


def cmd_launchd_install(args: argparse.Namespace) -> int:
    command_path = shutil_lib.which("codex-auth-pool")
    if not command_path:
        raise SystemExit("codex-auth-pool command not found in PATH; activate/install the package first")
    args.stdout_path.parent.mkdir(parents=True, exist_ok=True)
    args.stdout_path.write_text("")
    args.stderr_path.write_text("")
    plist_path = write_launchd_plist(
        label=args.label,
        command_path=command_path,
        stdout_path=args.stdout_path,
        stderr_path=args.stderr_path,
        interval_seconds=args.interval_seconds,
        state_path=args.state_path,
        target=args.target,
        sessions_dir=args.sessions_dir,
        source_dir=args.source_dir,
        managed_dir=args.managed_dir,
        events_path=args.events_path,
        primary_threshold=args.primary_threshold,
        secondary_threshold=args.secondary_threshold,
        restart_after_switch=args.restart_after_switch,
        app_path=Path(args.app_path),
        refresh_usage=args.refresh_usage,
        usage_max_age_minutes=args.usage_max_age_minutes,
    )
    launchctl_bootout(args.label)
    launchctl_bootstrap(args.label, plist_path)
    print(f"installed launchd agent at {plist_path}")
    status = launchctl_status(args.label)
    append_event(
        args.events_path,
        "launchd_install",
        label=args.label,
        plist_path=str(plist_path),
    )
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def cmd_launchd_start(args: argparse.Namespace) -> int:
    plist_path = launchd_plist_path(args.label)
    if not plist_path.exists():
        raise SystemExit(f"launchd plist not found: {plist_path}")
    status = launchctl_status(args.label)
    if status["loaded"]:
        launchctl_kickstart(args.label)
    else:
        launchctl_bootstrap(args.label, plist_path)
    print(json.dumps(launchctl_status(args.label), ensure_ascii=False, indent=2))
    return 0


def cmd_launchd_stop(args: argparse.Namespace) -> int:
    launchctl_bootout(args.label)
    print(json.dumps(launchctl_status(args.label), ensure_ascii=False, indent=2))
    return 0


def cmd_launchd_uninstall(args: argparse.Namespace) -> int:
    plist_path = launchd_plist_path(args.label)
    launchctl_bootout(args.label)
    if plist_path.exists():
        plist_path.unlink()
    print(json.dumps(launchctl_status(args.label), ensure_ascii=False, indent=2))
    return 0


def cmd_launchd_status(args: argparse.Namespace) -> int:
    status = launchctl_status(args.label)
    stdout_line = read_recent_log_line(DEFAULT_LAUNCHD_STDOUT)
    stderr_line = read_recent_log_line(DEFAULT_LAUNCHD_STDERR)
    stdout_time = file_mtime(DEFAULT_LAUNCHD_STDOUT)
    stderr_time = file_mtime(DEFAULT_LAUNCHD_STDERR)
    print("Launchd")
    print(f"  label: {status['label']}")
    print(f"  installed: {'yes' if status['installed'] else 'no'}")
    print(f"  loaded: {'yes' if status['loaded'] else 'no'}")
    print(f"  state: {status['state'] or '-'}")
    print(f"  pid: {status['pid'] or '-'}")
    print(f"  plist: {status['plist_path']}")
    print(f"  recent stdout: {stdout_line or '-'}")
    print(f"  stdout time: {fmt_dt(stdout_time)}")
    print(f"  recent stderr: {stderr_line or '-'}")
    print(f"  stderr time: {fmt_dt(stderr_time)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert cliproxyapi Codex auth files into Codex Desktop auth.json format."
    )
    parser.set_defaults(func=None)
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_CLIPROXY_DIR,
        help=f"cliproxyapi auth directory (default: {DEFAULT_CLIPROXY_DIR})",
    )
    parser.add_argument(
        "--export-dir",
        type=Path,
        default=DEFAULT_EXPORT_DIR,
        help=f"default export directory (default: {DEFAULT_EXPORT_DIR})",
    )
    parser.add_argument(
        "--managed-dir",
        type=Path,
        default=DEFAULT_MANAGED_DIR,
        help=f"managed official-login profile directory (default: {DEFAULT_MANAGED_DIR})",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"pool state path (default: {DEFAULT_STATE_PATH})",
    )
    parser.add_argument(
        "--config-path",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"config path (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--events-path",
        type=Path,
        default=DEFAULT_EVENTS_PATH,
        help=f"event log path (default: {DEFAULT_EVENTS_PATH})",
    )
    parser.add_argument(
        "--env-snapshots-dir",
        type=Path,
        default=DEFAULT_ENV_SNAPSHOTS_DIR,
        help=f"environment snapshot directory (default: {DEFAULT_ENV_SNAPSHOTS_DIR})",
    )
    parser.add_argument(
        "--target",
        default=str(DEFAULT_CODEX_AUTH_PATH),
        help=f"Codex auth target path (default: {DEFAULT_CODEX_AUTH_PATH})",
    )
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        default=DEFAULT_CODEX_SESSIONS_DIR,
        help=f"Codex sessions directory (default: {DEFAULT_CODEX_SESSIONS_DIR})",
    )
    parser.add_argument(
        "--app-path",
        default=str(DEFAULT_CODEX_APP),
        help=f"Codex app path (default: {DEFAULT_CODEX_APP})",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="disable macOS notifications for switch/save/import events",
    )

    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list", help="list available auth profiles from cliproxyapi and the managed vault")
    list_parser.set_defaults(func=cmd_list)

    status_parser = subparsers.add_parser("status", help="show ranked pool status and cooldown state")
    status_parser.set_defaults(func=cmd_status)

    pick_parser = subparsers.add_parser("pick", help="print the currently preferred profile")
    pick_parser.set_defaults(func=cmd_pick)

    dashboard_parser = subparsers.add_parser(
        "dashboard",
        help="show a human-friendly one-screen overview of current account, limits, next candidate, and daemon health",
    )
    dashboard_parser.set_defaults(func=cmd_dashboard)

    info_parser = subparsers.add_parser(
        "info",
        help="show where the tool is installed and which global home-directory paths it actually manages",
    )
    info_parser.set_defaults(func=cmd_info)

    check_parser = subparsers.add_parser(
        "check",
        help="run a friendly health check for auth sync, profiles, snapshots, config, and launchd state",
    )
    check_parser.set_defaults(func=cmd_check)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="detect common Codex, cliproxyapi, vault, app, and session paths on this machine",
    )
    doctor_parser.set_defaults(func=cmd_doctor)

    init_parser = subparsers.add_parser(
        "init",
        help="one-command first-run onboarding: config, baseline snapshot, save current login, optional launchd",
    )
    init_parser.add_argument(
        "--snapshot-env",
        action="store_true",
        default=True,
        help="capture a baseline local environment snapshot during init (default: enabled)",
    )
    init_parser.add_argument(
        "--no-snapshot-env",
        action="store_false",
        dest="snapshot_env",
        help="skip the baseline local environment snapshot",
    )
    init_parser.add_argument(
        "--snapshot-name",
        default="baseline",
        help="snapshot name used when --snapshot-env is enabled (default: baseline)",
    )
    init_parser.add_argument(
        "--save-current",
        action="store_true",
        default=True,
        help="save the current official Codex login into the managed vault if present (default: enabled)",
    )
    init_parser.add_argument(
        "--no-save-current",
        action="store_false",
        dest="save_current",
        help="skip saving the current official login",
    )
    init_parser.add_argument(
        "--profile-name",
        default="official-current",
        help="managed profile name used with --save-current (default: official-current)",
    )
    init_parser.add_argument(
        "--sync-cliproxy",
        action="store_true",
        default=True,
        help="import cliproxy source accounts into the managed Codex-format vault (default: enabled)",
    )
    init_parser.add_argument(
        "--no-sync-cliproxy",
        action="store_false",
        dest="sync_cliproxy",
        help="skip syncing cliproxy source accounts into the managed vault",
    )
    init_parser.add_argument(
        "--install-launchd",
        action="store_true",
        help="also install the background launchd agent",
    )
    init_parser.add_argument("--interval-seconds", type=int, default=60)
    init_parser.add_argument("--primary-threshold", type=float, default=DEFAULT_PRIMARY_THRESHOLD)
    init_parser.add_argument("--secondary-threshold", type=float, default=DEFAULT_SECONDARY_THRESHOLD)
    init_parser.add_argument("--restart-after-switch", action="store_true")
    init_parser.add_argument(
        "--no-notify",
        action="store_true",
        help="disable notifications for init-triggered save and snapshot steps",
    )
    init_parser.set_defaults(func=cmd_init)

    env_status_parser = subparsers.add_parser(
        "env-status",
        help="show tracked local plugin/connector/config state and available environment snapshots",
    )
    env_status_parser.set_defaults(func=cmd_env_status)

    snapshot_env_parser = subparsers.add_parser(
        "snapshot-env",
        help="save a reusable snapshot of local Codex config, plugins, and connector cache state",
    )
    snapshot_env_parser.add_argument("--name", help="snapshot name; default is timestamp-based")
    snapshot_env_parser.add_argument("--no-notify", action="store_true", help="disable the snapshot notification")
    snapshot_env_parser.set_defaults(func=cmd_snapshot_env)

    restore_env_parser = subparsers.add_parser(
        "restore-env",
        help="restore a saved local environment snapshot and auto-back up the current state first",
    )
    restore_env_parser.add_argument(
        "snapshot_dir",
        nargs="?",
        help="snapshot directory name or path; default is the latest snapshot",
    )
    restore_env_parser.add_argument(
        "--restart-codex",
        action="store_true",
        help="restart Codex Desktop after restoring local environment state",
    )
    restore_env_parser.add_argument(
        "--graceful-restart",
        action="store_true",
        help="use AppleScript quit instead of hard process kill before reopening",
    )
    restore_env_parser.add_argument("--no-notify", action="store_true", help="disable the restore notification")
    restore_env_parser.set_defaults(func=cmd_restore_env)

    config_init_parser = subparsers.add_parser(
        "config-init",
        help="write a starter config file based on detected local paths",
    )
    config_init_parser.set_defaults(func=cmd_config_init)

    migrate_managed_parser = subparsers.add_parser(
        "migrate-managed",
        help="convert existing managed-vault profiles into native Codex auth files plus sidecar metadata",
    )
    migrate_managed_parser.set_defaults(func=cmd_migrate_managed)

    sync_cliproxy_parser = subparsers.add_parser(
        "sync-cliproxy",
        help="import every cliproxy source account into the managed vault as native Codex auth files",
    )
    sync_cliproxy_parser.set_defaults(func=cmd_sync_cliproxy)

    set_weekly_reset_parser = subparsers.add_parser(
        "set-weekly-reset",
        help="manually correct a profile's weekly reset time when upstream auth data is wrong or missing",
    )
    set_weekly_reset_parser.add_argument("profile", help="profile path, filename, or unique substring")
    set_weekly_reset_parser.add_argument("when", nargs="?", help="ISO 8601 datetime, e.g. 2026-04-19T03:25:32+08:00")
    set_weekly_reset_parser.add_argument("--clear", action="store_true", help="clear the stored weekly reset time")
    set_weekly_reset_parser.set_defaults(func=cmd_set_weekly_reset)

    prefer_next_parser = subparsers.add_parser(
        "prefer-next",
        help="manually choose which account should be used next when you know the preferred next round account",
    )
    prefer_next_parser.add_argument("profile", nargs="?", help="profile path, filename, or unique substring")
    prefer_next_parser.add_argument("--clear", action="store_true", help="clear the preferred next account")
    prefer_next_parser.set_defaults(func=cmd_prefer_next)

    refresh_usage_parser = subparsers.add_parser(
        "refresh-usage",
        help="query ChatGPT directly for each account's real 5h and weekly windows, then cache those reset times into metadata",
    )
    refresh_usage_parser.add_argument("profile", nargs="?", help="optional single profile path, filename, or unique substring")
    refresh_usage_parser.add_argument("--force", action="store_true", help="refresh even if the cached usage observation is still fresh")
    refresh_usage_parser.add_argument(
        "--max-age-minutes",
        type=int,
        default=30,
        help="skip profiles whose usage observation is newer than this many minutes unless --force is set",
    )
    refresh_usage_parser.set_defaults(func=cmd_refresh_usage)

    setup_parser = subparsers.add_parser(
        "setup",
        help="one-shot onboarding: write config and optionally install the background launchd agent",
    )
    setup_parser.add_argument("--install-launchd", action="store_true", help="also install the launchd background agent")
    setup_parser.add_argument("--interval-seconds", type=int, default=60)
    setup_parser.add_argument("--primary-threshold", type=float, default=DEFAULT_PRIMARY_THRESHOLD)
    setup_parser.add_argument("--secondary-threshold", type=float, default=DEFAULT_SECONDARY_THRESHOLD)
    setup_parser.add_argument("--restart-after-switch", action="store_true")
    setup_parser.add_argument(
        "--snapshot-env",
        action="store_true",
        help="also capture a baseline local environment snapshot during setup",
    )
    setup_parser.add_argument(
        "--snapshot-name",
        help="optional snapshot name to use with --snapshot-env",
    )
    setup_parser.set_defaults(func=cmd_setup)

    events_parser = subparsers.add_parser(
        "events",
        help="show recent save/import/cooldown/switch events from the auth-pool event log",
    )
    events_parser.add_argument("--limit", type=int, default=20)
    events_parser.set_defaults(func=cmd_events)

    save_current_parser = subparsers.add_parser(
        "save-current",
        help="save the current official Codex login into the managed vault so later logins do not overwrite it",
    )
    save_current_parser.add_argument("--name", help="managed profile name, e.g. my-plus-1")
    save_current_parser.add_argument("--no-notify", action="store_true", help="disable the save notification")
    save_current_parser.set_defaults(func=cmd_save_current)

    import_auth_parser = subparsers.add_parser(
        "import-auth-file",
        help="import any official Codex auth.json or cliproxyapi auth file into the managed vault",
    )
    import_auth_parser.add_argument("auth_file", help="path to an auth file")
    import_auth_parser.add_argument("--name", help="managed profile name")
    import_auth_parser.add_argument(
        "--source-kind",
        choices=["managed", "cliproxyapi", "manual"],
        help="optional source tag stored with the imported profile",
    )
    import_auth_parser.add_argument("--no-notify", action="store_true", help="disable the import notification")
    import_auth_parser.set_defaults(func=cmd_import_auth_file)

    rate_limits_parser = subparsers.add_parser(
        "rate-limits", help="show the latest Codex rate-limit snapshot parsed from session logs"
    )
    rate_limits_parser.set_defaults(func=cmd_rate_limits)

    preview_parser = subparsers.add_parser("preview", help="preview a converted auth payload")
    preview_parser.add_argument("profile", help="profile path, filename, or unique substring")
    preview_parser.set_defaults(func=cmd_preview)

    export_parser = subparsers.add_parser("export", help="export a converted auth payload to a file")
    export_parser.add_argument("profile", help="profile path, filename, or unique substring")
    export_parser.add_argument(
        "--output",
        help="explicit output path; default is under --export-dir",
    )
    export_parser.set_defaults(func=cmd_export)

    apply_parser = subparsers.add_parser(
        "apply",
        help="backup and replace Codex Desktop auth.json with a converted profile",
    )
    apply_parser.add_argument("profile", help="profile path, filename, or unique substring")
    apply_parser.add_argument(
        "--restart-after-switch",
        action="store_true",
        help="restart Codex Desktop automatically after switching auth",
    )
    apply_parser.add_argument(
        "--graceful-restart",
        action="store_true",
        help="use AppleScript quit instead of hard process kill before reopening",
    )
    apply_parser.set_defaults(func=cmd_apply)

    apply_best_parser = subparsers.add_parser(
        "apply-best",
        help="pick the best currently available profile and apply it",
    )
    apply_best_parser.add_argument(
        "--restart-after-switch",
        action="store_true",
        help="restart Codex Desktop automatically after switching auth",
    )
    apply_best_parser.add_argument(
        "--graceful-restart",
        action="store_true",
        help="use AppleScript quit instead of hard process kill before reopening",
    )
    apply_best_parser.set_defaults(func=cmd_apply_best)

    cooldown_parser = subparsers.add_parser(
        "cooldown",
        help="mark a profile unavailable for a cooldown window, default 5 hours",
    )
    cooldown_parser.add_argument("profile", help="profile path, filename, or unique substring")
    cooldown_parser.add_argument("--hours", type=float, default=5.0, help="cooldown length in hours")
    cooldown_parser.add_argument("--reason", default="quota_5h_window", help="cooldown reason label")
    cooldown_parser.add_argument("--clear", action="store_true", help="clear cooldown instead of setting it")
    cooldown_parser.set_defaults(func=cmd_cooldown)

    tick_parser = subparsers.add_parser(
        "tick",
        help="inspect recent Codex rate limits, cool down the current account if exhausted, and optionally switch",
    )
    tick_parser.add_argument(
        "--primary-threshold",
        type=float,
        default=DEFAULT_PRIMARY_THRESHOLD,
        help="5-hour window used_percent threshold that triggers rotation",
    )
    tick_parser.add_argument(
        "--secondary-threshold",
        type=float,
        default=DEFAULT_SECONDARY_THRESHOLD,
        help="weekly window used_percent threshold that triggers rotation",
    )
    tick_parser.add_argument(
        "--no-apply-best",
        action="store_true",
        help="only mark cooldown; do not switch to another account",
    )
    tick_parser.add_argument(
        "--restart-after-switch",
        action="store_true",
        help="restart Codex Desktop automatically after switching auth",
    )
    tick_parser.add_argument(
        "--graceful-restart",
        action="store_true",
        help="use AppleScript quit instead of hard process kill before reopening",
    )
    tick_parser.add_argument(
        "--refresh-usage",
        action="store_true",
        default=True,
        help="refresh per-account real usage windows before ranking and rotation (default: enabled)",
    )
    tick_parser.add_argument(
        "--no-refresh-usage",
        action="store_false",
        dest="refresh_usage",
        help="skip the direct per-account usage refresh step",
    )
    tick_parser.add_argument(
        "--usage-max-age-minutes",
        type=int,
        default=30,
        help="consider cached per-account usage observations stale after this many minutes",
    )
    tick_parser.set_defaults(func=cmd_tick)

    daemon_parser = subparsers.add_parser(
        "daemon",
        help="run tick repeatedly to keep the Codex auth pool rotating automatically",
    )
    daemon_parser.add_argument("--interval-seconds", type=int, default=60, help="tick interval in seconds")
    daemon_parser.add_argument(
        "--primary-threshold",
        type=float,
        default=DEFAULT_PRIMARY_THRESHOLD,
        help="5-hour window used_percent threshold that triggers rotation",
    )
    daemon_parser.add_argument(
        "--secondary-threshold",
        type=float,
        default=DEFAULT_SECONDARY_THRESHOLD,
        help="weekly window used_percent threshold that triggers rotation",
    )
    daemon_parser.add_argument(
        "--no-apply-best",
        action="store_true",
        help="only mark cooldown; do not switch to another account",
    )
    daemon_parser.add_argument(
        "--restart-after-switch",
        action="store_true",
        help="restart Codex Desktop automatically after switching auth",
    )
    daemon_parser.add_argument(
        "--graceful-restart",
        action="store_true",
        help="use AppleScript quit instead of hard process kill before reopening",
    )
    daemon_parser.add_argument(
        "--refresh-usage",
        action="store_true",
        default=True,
        help="refresh per-account real usage windows before each rotation check (default: enabled)",
    )
    daemon_parser.add_argument(
        "--no-refresh-usage",
        action="store_false",
        dest="refresh_usage",
        help="skip the direct per-account usage refresh step",
    )
    daemon_parser.add_argument(
        "--usage-max-age-minutes",
        type=int,
        default=30,
        help="consider cached per-account usage observations stale after this many minutes",
    )
    daemon_parser.set_defaults(func=cmd_daemon)

    restart_parser = subparsers.add_parser("restart-codex", help="restart Codex Desktop now")
    restart_parser.add_argument(
        "--graceful-restart",
        action="store_true",
        help="use AppleScript quit instead of hard process kill before reopening",
    )
    restart_parser.set_defaults(func=cmd_restart_codex)

    launchd_install_parser = subparsers.add_parser(
        "launchd-install",
        help="install a user-level launchd agent for automatic auth-pool rotation",
    )
    launchd_install_parser.add_argument("--label", default=DEFAULT_LAUNCHD_LABEL)
    launchd_install_parser.add_argument("--interval-seconds", type=int, default=60)
    launchd_install_parser.add_argument("--primary-threshold", type=float, default=DEFAULT_PRIMARY_THRESHOLD)
    launchd_install_parser.add_argument("--secondary-threshold", type=float, default=DEFAULT_SECONDARY_THRESHOLD)
    launchd_install_parser.add_argument(
        "--refresh-usage",
        action="store_true",
        default=True,
        help="refresh per-account real usage windows before each daemon rotation check (default: enabled)",
    )
    launchd_install_parser.add_argument(
        "--no-refresh-usage",
        action="store_false",
        dest="refresh_usage",
        help="skip direct per-account usage refreshes in the launchd daemon",
    )
    launchd_install_parser.add_argument(
        "--usage-max-age-minutes",
        type=int,
        default=30,
        help="consider cached per-account usage observations stale after this many minutes",
    )
    launchd_install_parser.add_argument(
        "--restart-after-switch",
        action="store_true",
        help="restart Codex Desktop automatically after switching auth",
    )
    launchd_install_parser.add_argument(
        "--stdout-path",
        type=Path,
        default=DEFAULT_LAUNCHD_STDOUT,
        help=f"launchd stdout log path (default: {DEFAULT_LAUNCHD_STDOUT})",
    )
    launchd_install_parser.add_argument(
        "--stderr-path",
        type=Path,
        default=DEFAULT_LAUNCHD_STDERR,
        help=f"launchd stderr log path (default: {DEFAULT_LAUNCHD_STDERR})",
    )
    launchd_install_parser.set_defaults(func=cmd_launchd_install)

    launchd_start_parser = subparsers.add_parser("launchd-start", help="start or kickstart the launchd agent")
    launchd_start_parser.add_argument("--label", default=DEFAULT_LAUNCHD_LABEL)
    launchd_start_parser.set_defaults(func=cmd_launchd_start)

    launchd_stop_parser = subparsers.add_parser("launchd-stop", help="stop the launchd agent")
    launchd_stop_parser.add_argument("--label", default=DEFAULT_LAUNCHD_LABEL)
    launchd_stop_parser.set_defaults(func=cmd_launchd_stop)

    launchd_uninstall_parser = subparsers.add_parser("launchd-uninstall", help="remove the launchd agent")
    launchd_uninstall_parser.add_argument("--label", default=DEFAULT_LAUNCHD_LABEL)
    launchd_uninstall_parser.set_defaults(func=cmd_launchd_uninstall)

    launchd_status_parser = subparsers.add_parser("launchd-status", help="show launchd agent status")
    launchd_status_parser.add_argument("--label", default=DEFAULT_LAUNCHD_LABEL)
    launchd_status_parser.set_defaults(func=cmd_launchd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args = apply_runtime_defaults(args)
    if args.func is None:
        parser.print_help(sys.stderr)
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

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
import contextlib
import json
import os
import platform
import re
import shlex
import shutil as shutil_lib
import shutil
import sqlite3
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

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


DEFAULT_CLIPROXY_DIR = Path.home() / ".cli-proxy-api"
DEFAULT_CODEX_AUTH_PATH = Path.home() / ".codex" / "cache" / "auth.json"
DEFAULT_CODEX_ROOT_AUTH_PATH = Path.home() / ".codex" / "auth.json"
DEFAULT_CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
DEFAULT_CODEX_STATE_DB = Path.home() / ".codex" / "state_5.sqlite"
DEFAULT_CODEX_LOGS_DB = Path.home() / ".codex" / "logs_2.sqlite"
DEFAULT_EXPORT_DIR = Path.home() / ".codex-auth-pool" / "exports"
DEFAULT_MANAGED_DIR = Path.home() / ".codex-auth-pool" / "profiles"
DEFAULT_SOURCE_META_DIR = Path.home() / ".codex-auth-pool" / "source-meta"
DEFAULT_STATE_PATH = Path.home() / ".codex-auth-pool" / "state.json"
DEFAULT_CONFIG_PATH = Path.home() / ".codex-auth-pool" / "config.json"
DEFAULT_EVENTS_PATH = Path.home() / ".codex-auth-pool" / "events.jsonl"
DEFAULT_ENV_SNAPSHOTS_DIR = Path.home() / ".codex-auth-pool" / "env-snapshots"
DEFAULT_SESSION_RECOVERY_DIR = Path.home() / ".codex-auth-pool" / "session-recovery"
DEFAULT_TICK_LOCK_PATH = Path.home() / ".codex-auth-pool" / "run" / "tick.lock"
DEFAULT_APPLY_LOCK_PATH = Path.home() / ".codex-auth-pool" / "run" / "apply.lock"
DEFAULT_CODEX_APP = Path("/Applications/Codex.app") if platform.system() == "Darwin" else Path("")
DEFAULT_LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
DEFAULT_LAUNCHD_LABEL = "ai.codex.auth.pool"
DEFAULT_LAUNCHD_STDOUT = Path.home() / ".codex-auth-pool" / "logs" / "launchd.stdout.log"
DEFAULT_LAUNCHD_STDERR = Path.home() / ".codex-auth-pool" / "logs" / "launchd.stderr.log"
DEFAULT_SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"
DEFAULT_SYSTEMD_SERVICE = "codex-auth-pool.service"
DEFAULT_SYSTEMD_STDOUT = Path.home() / ".codex-auth-pool" / "logs" / "systemd.stdout.log"
DEFAULT_SYSTEMD_STDERR = Path.home() / ".codex-auth-pool" / "logs" / "systemd.stderr.log"
BACKUP_TIMESTAMP_FMT = "%Y%m%d-%H%M%S"
ENV_VAR_MAP = {
    "source_dir": "CODEX_AUTH_POOL_SOURCE_DIR",
    "managed_dir": "CODEX_AUTH_POOL_MANAGED_DIR",
    "state_path": "CODEX_AUTH_POOL_STATE_PATH",
    "config_path": "CODEX_AUTH_POOL_CONFIG_PATH",
    "events_path": "CODEX_AUTH_POOL_EVENTS_PATH",
    "env_snapshots_dir": "CODEX_AUTH_POOL_ENV_SNAPSHOTS_DIR",
    "session_recovery_dir": "CODEX_AUTH_POOL_SESSION_RECOVERY_DIR",
    "target": "CODEX_AUTH_POOL_TARGET",
    "sessions_dir": "CODEX_AUTH_POOL_SESSIONS_DIR",
    "codex_state_db": "CODEX_AUTH_POOL_CODEX_STATE_DB",
    "codex_logs_db": "CODEX_AUTH_POOL_CODEX_LOGS_DB",
    "app_path": "CODEX_AUTH_POOL_APP_PATH",
}
DEFAULT_ARG_VALUES = {
    "source_dir": DEFAULT_CLIPROXY_DIR,
    "managed_dir": DEFAULT_MANAGED_DIR,
    "state_path": DEFAULT_STATE_PATH,
    "config_path": DEFAULT_CONFIG_PATH,
    "events_path": DEFAULT_EVENTS_PATH,
    "env_snapshots_dir": DEFAULT_ENV_SNAPSHOTS_DIR,
    "session_recovery_dir": DEFAULT_SESSION_RECOVERY_DIR,
    "target": str(DEFAULT_CODEX_AUTH_PATH),
    "sessions_dir": DEFAULT_CODEX_SESSIONS_DIR,
    "codex_state_db": DEFAULT_CODEX_STATE_DB,
    "codex_logs_db": DEFAULT_CODEX_LOGS_DB,
    "app_path": str(DEFAULT_CODEX_APP),
}
REQUIRED_SOURCE_KEYS = (
    "access_token",
    "refresh_token",
    "id_token",
    "account_id",
)
DEFAULT_PRIMARY_THRESHOLD = 99.0
DEFAULT_SECONDARY_THRESHOLD = 99.0
DEFAULT_USAGE_MAX_AGE_MINUTES = 30
MIN_AUTO_RESTART_INTERVAL_SECONDS = 120
AUTO_DISCOVERY_MAX_INITIAL_USAGE_REFRESHES = 5
DEFAULT_INTERRUPTED_SESSION_WINDOW_SECONDS = 24 * 60 * 60
DEFAULT_INTERRUPTED_SESSION_MAX_COUNT = 30
DEFAULT_INTERRUPTED_SESSION_PROMPT = "继续"
DEFAULT_INTERRUPTED_SESSION_RETRY_PROMPT = "继续。如果你刚才只发送了确认文本，现在不要再确认，直接从上次中断点继续实际执行。"
RECENT_SESSION_ACTIVITY_GRACE_SECONDS = 180
RESUME_VERIFY_DELAY_SECONDS = 12.0
RESUME_RETRY_ATTEMPTS = 2


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


@dataclass
class InterruptedSession:
    id: str
    title: str
    cwd: str
    source: str
    rollout_path: str
    updated_at: int
    last_log_at: int | None
    recent_log_count: int


def now_local() -> datetime:
    return datetime.now().astimezone()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
        return datetime.fromisoformat(normalized)
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
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise SystemExit(f"file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid json in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"expected JSON object in {path}, got {type(data).__name__}")
    return data


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
        "session_recovery_dir",
        "sessions_dir",
        "codex_state_db",
        "codex_logs_db",
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


def codex_process_running() -> bool:
    checks = [
        ["pgrep", "-x", "Codex"],
        ["pgrep", "-f", "/Applications/Codex.app/Contents/Frameworks/Codex Helper"],
        ["pgrep", "-f", "SkyComputerUseClient"],
    ]
    return any(run_command(command).returncode == 0 for command in checks)


def wait_for_codex_state(*, running: bool, timeout_seconds: float, poll_seconds: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if codex_process_running() is running:
            return True
        time.sleep(poll_seconds)
    return codex_process_running() is running


def stop_codex_app(*, graceful_first: bool, wait_seconds: float) -> None:
    if graceful_first:
        run_command(["osascript", "-e", 'tell application "Codex" to quit'])
        if wait_for_codex_state(running=False, timeout_seconds=max(wait_seconds, 8.0)):
            return

    for signal_flag in ([], ["-9"]):
        run_command(["pkill", *signal_flag, "-x", "Codex"])
        run_command(["pkill", *signal_flag, "-f", "/Applications/Codex.app/Contents/Frameworks/Codex Helper"])
        run_command(["pkill", *signal_flag, "-f", "SkyComputerUseClient"])
        if wait_for_codex_state(running=False, timeout_seconds=max(wait_seconds, 5.0)):
            return

    raise SystemExit("failed to stop Codex cleanly before restart")


def is_macos() -> bool:
    return platform.system() == "Darwin"


def is_linux() -> bool:
    return platform.system() == "Linux"


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
    if is_macos() and shutil_lib.which("osascript"):
        script = f'display notification "{message.replace(chr(34), chr(39))}" with title "{title.replace(chr(34), chr(39))}"'
        run_command(["osascript", "-e", script])
        return
    if is_linux() and shutil_lib.which("notify-send"):
        run_command(["notify-send", title, message])


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


def read_recent_event_of_type(events_path: Path, event_type: str) -> dict[str, Any] | None:
    if not events_path.exists():
        return None
    lines = events_path.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event_type") == event_type:
            return event
    return None


def recent_resumed_sessions_summary(events_path: Path, limit: int = 3) -> dict[str, Any] | None:
    event = read_recent_event_of_type(events_path, "interrupted_sessions_resume_started")
    if event is None:
        return None
    sessions = event.get("sessions")
    if not isinstance(sessions, list):
        sessions = []
    titles = [
        str(session.get("title") or session.get("session_id") or "-")
        for session in sessions[:limit]
        if isinstance(session, dict)
    ]
    return {
        "timestamp": event.get("timestamp"),
        "session_count": int(event.get("session_count") or len(sessions)),
        "titles": titles,
        "snapshot_path": event.get("snapshot_path"),
    }


def recent_restarted_terminal_summary(events_path: Path) -> dict[str, Any] | None:
    event = read_recent_event_of_type(events_path, "interrupted_terminal_commands_restarted")
    if event is None:
        return None
    return {
        "timestamp": event.get("timestamp"),
        "command_count": int(event.get("command_count") or 0),
    }


def recent_session_only_recovery_summary(events_path: Path) -> dict[str, Any] | None:
    event = read_recent_event_of_type(events_path, "interrupted_terminal_commands_not_restarted")
    if event is None:
        return None
    return {
        "timestamp": event.get("timestamp"),
        "reason": event.get("reason") or "session_owner_resume_only",
        "session_count": int(event.get("session_count") or 0),
    }


def recent_resume_verification_summary(events_path: Path) -> dict[str, Any] | None:
    event = read_recent_event_of_type(events_path, "interrupted_sessions_resume_verified")
    if event is None:
        return None
    return {
        "timestamp": event.get("timestamp"),
        "session_count": int(event.get("session_count") or 0),
        "active_count": int(event.get("active_count") or 0),
    }


def recent_interrupted_capture_summary(events_path: Path) -> dict[str, Any] | None:
    event = read_recent_event_of_type(events_path, "interrupted_sessions_captured")
    if event is None:
        return None
    return {
        "timestamp": event.get("timestamp"),
        "session_count": int(event.get("session_count") or 0),
        "recent_candidates": (
            int(event.get("recent_candidates"))
            if "recent_candidates" in event and event.get("recent_candidates") is not None
            else None
        ),
        "filtered_terminal_sessions": (
            int(event.get("filtered_terminal_sessions"))
            if "filtered_terminal_sessions" in event and event.get("filtered_terminal_sessions") is not None
            else None
        ),
    }


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


@contextlib.contextmanager
def exclusive_lock(path: Path, *, blocking: bool = False):
    if fcntl is None:
        yield True
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def restart_recently_blocked(state: dict[str, Any], *, now: datetime) -> tuple[bool, int | None]:
    last_restart = parse_dt(state.get("last_restart_at"))
    if last_restart is None:
        return False, None
    elapsed = int((now - last_restart).total_seconds())
    if elapsed < MIN_AUTO_RESTART_INTERVAL_SECONDS:
        return True, max(0, MIN_AUTO_RESTART_INTERVAL_SECONDS - elapsed)
    return False, None


def auto_rotation_recently_blocked(state: dict[str, Any], *, now: datetime) -> tuple[bool, int | None]:
    last_rotation = parse_dt(state.get("last_auto_rotation_at"))
    if last_rotation is None:
        return False, None
    elapsed = int((now - last_rotation).total_seconds())
    if elapsed < MIN_AUTO_RESTART_INTERVAL_SECONDS:
        return True, max(0, MIN_AUTO_RESTART_INTERVAL_SECONDS - elapsed)
    return False, None


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


def _connect_sqlite_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)


def read_recent_desktop_sessions(
    *,
    codex_state_db: Path,
    codex_logs_db: Path,
    max_age_seconds: int,
    max_count: int,
) -> list[InterruptedSession]:
    if not codex_state_db.exists():
        return []

    cutoff = int(time.time()) - max_age_seconds
    sessions: list[InterruptedSession] = []
    try:
        with _connect_sqlite_readonly(codex_state_db) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    id,
                    title,
                    cwd,
                    source,
                    rollout_path,
                    updated_at,
                    NULL AS last_log_at,
                    0 AS recent_log_count
                FROM threads
                WHERE
                    archived = 0
                    AND source IN ('vscode', 'desktop', 'app')
                    AND updated_at >= ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (cutoff, max_count),
            ).fetchall()
    except sqlite3.Error:
        return []

    for row in rows:
        sessions.append(
            InterruptedSession(
                id=str(row["id"]),
                title=str(row["title"] or ""),
                cwd=str(row["cwd"] or ""),
                source=str(row["source"] or ""),
                rollout_path=str(row["rollout_path"] or ""),
                updated_at=int(row["updated_at"] or 0),
                last_log_at=int(row["last_log_at"]) if row["last_log_at"] is not None else None,
                recent_log_count=int(row["recent_log_count"] or 0),
            )
        )
    return sessions


def _session_event_kind(event: dict[str, Any]) -> str | None:
    event_type = event.get("type")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    payload_type = payload.get("type")
    if event_type == "event_msg" and payload_type == "task_complete":
        return "task_complete"
    if event_type == "event_msg" and payload_type == "agent_message":
        return "active"
    if event_type == "response_item" and payload_type in {"function_call", "function_call_output", "reasoning"}:
        return "active"
    if event_type == "event_msg" and payload_type in {"exec_command_end", "exec_command_start"}:
        return "active"
    return None


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _tool_call_summary(payload: dict[str, Any]) -> dict[str, Any] | None:
    name = str(payload.get("name") or "")
    call_id = str(payload.get("call_id") or "")
    arguments = _parse_json_object(payload.get("arguments"))
    if not name or not call_id:
        return None
    summary: dict[str, Any] = {
        "call_id": call_id,
        "name": name,
        "commands": [],
    }
    if name == "exec_command":
        summary["commands"].append(
            {
                "cmd": arguments.get("cmd"),
                "workdir": arguments.get("workdir"),
            }
        )
    elif name == "write_stdin":
        summary["terminal_session_id"] = arguments.get("session_id")
    elif name == "parallel":
        for tool_use in arguments.get("tool_uses", []):
            if not isinstance(tool_use, dict):
                continue
            if tool_use.get("recipient_name") != "functions.exec_command":
                continue
            parameters = tool_use.get("parameters") if isinstance(tool_use.get("parameters"), dict) else {}
            summary["commands"].append(
                {
                    "cmd": parameters.get("cmd"),
                    "workdir": parameters.get("workdir"),
                }
            )
    return summary


def _terminal_session_id_from_output(output: Any) -> str | None:
    if not isinstance(output, str):
        return None
    match = re.search(r"session ID\s+([0-9]+)", output)
    return match.group(1) if match else None


def _output_says_process_exited(output: Any) -> bool:
    return isinstance(output, str) and "Process exited with code" in output


def _command_first_segment(command: str) -> str:
    return re.split(r"\s*(?:&&|\|\||;)\s*", command.strip(), maxsplit=1)[0].strip()


def _is_sleep_only_command(command: str) -> bool:
    first = _command_first_segment(command)
    return bool(re.match(r"^(?:rtk\s+)?sleep\b", first))


def _is_observational_command(command: str) -> bool:
    lowered = command.lower()
    observational_patterns = (
        r"\bgrep\b",
        r"\brg\b",
        r"\btail\b",
        r"\bhead\b",
        r"\bsed\b",
        r"\bawk\b",
        r"\bcat\b",
        r"\bls\b",
        r"\bps\b",
        r"\bfind\b",
        r"\bnvidia-smi\b",
        r"\bifconfig\b",
        r"\bnetstat\b",
    )
    return any(re.search(pattern, lowered) for pattern in observational_patterns)


def _is_restartable_command(command: str) -> bool:
    cmd = " ".join(command.strip().split())
    if not cmd:
        return False
    if _is_sleep_only_command(cmd):
        return False
    lowered = cmd.lower()
    if "<<" in cmd:
        return False
    long_running_hints = (
        r"\b(?:bash|sh)\b.+\.sh\b",
        r"\bpython(?:3)?\b\s+[^\n]*\.py\b",
        r"\bpython(?:3)?\b\s+-m\b",
        r"\buv\s+run\b",
        r"\bpoetry\s+run\b",
        r"\bnode\b\s+[^\n]*\.(?:mjs|cjs|js)\b",
        r"\bnpm\s+run\b",
        r"\bpnpm\b",
        r"\byarn\b",
        r"\bmake\b",
        r"\bcodex\b",
        r"\bclaude\b",
    )
    if _is_observational_command(cmd) and not any(re.search(pattern, lowered) for pattern in long_running_hints):
        return False
    return any(re.search(pattern, lowered) for pattern in long_running_hints)


def _filter_restartable_tools(tools: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    filtered: list[dict[str, Any]] = []
    skipped_commands = 0
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        commands = tool.get("commands")
        if not isinstance(commands, list):
            continue
        kept_commands = []
        for command in commands:
            if not isinstance(command, dict):
                continue
            cmd = str(command.get("cmd") or "").strip()
            if not _is_restartable_command(cmd):
                skipped_commands += 1
                continue
            kept_commands.append(command)
        if kept_commands:
            updated_tool = dict(tool)
            updated_tool["commands"] = kept_commands
            filtered.append(updated_tool)
    return filtered, skipped_commands


def pending_tool_calls_at(session: InterruptedSession, captured_at: datetime) -> list[dict[str, Any]]:
    rollout_path = Path(session.rollout_path)
    if not rollout_path.exists() or not rollout_path.is_file():
        return []
    try:
        lines = rollout_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []

    pending: dict[str, dict[str, Any]] = {}
    call_summaries: dict[str, dict[str, Any]] = {}
    terminal_sessions: dict[str, dict[str, Any]] = {}
    write_call_sessions: dict[str, str] = {}
    for raw_line in lines:
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_at = parse_dt(str(event.get("timestamp") or ""))
        if event_at is not None and event_at > captured_at:
            break
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        event_type = event.get("type")
        payload_type = payload.get("type")
        call_id = str(payload.get("call_id") or "")
        if event_type == "response_item" and payload_type == "function_call":
            summary = _tool_call_summary(payload)
            if summary is not None:
                pending[str(summary["call_id"])] = summary
                call_summaries[str(summary["call_id"])] = summary
                if summary.get("name") == "write_stdin" and summary.get("terminal_session_id") is not None:
                    write_call_sessions[str(summary["call_id"])] = str(summary["terminal_session_id"])
        elif call_id and (
            (event_type == "response_item" and payload_type == "function_call_output")
            or (event_type == "event_msg" and str(payload_type).endswith("_end"))
        ):
            output = payload.get("output")
            original_summary = call_summaries.get(call_id, {})
            terminal_id = _terminal_session_id_from_output(output)
            if terminal_id and original_summary.get("commands"):
                terminal_sessions[terminal_id] = {
                    "call_id": call_id,
                    "name": "terminal_session",
                    "terminal_session_id": terminal_id,
                    "commands": original_summary.get("commands", []),
                }
            write_terminal_id = write_call_sessions.get(call_id)
            if write_terminal_id and _output_says_process_exited(output):
                terminal_sessions.pop(write_terminal_id, None)
            pending.pop(call_id, None)
        elif event_type == "event_msg" and payload_type == "task_complete":
            pending.clear()
            terminal_sessions.clear()
    combined: dict[str, dict[str, Any]] = {}
    for call_id, summary in pending.items():
        terminal_id = summary.get("terminal_session_id")
        if terminal_id is not None and str(terminal_id) in terminal_sessions:
            combined[f"terminal:{terminal_id}"] = terminal_sessions[str(terminal_id)]
        else:
            combined[f"call:{call_id}"] = summary
    for terminal_id, summary in terminal_sessions.items():
        combined.setdefault(f"terminal:{terminal_id}", summary)
    return list(combined.values())


def session_was_in_progress_at(session: InterruptedSession, captured_at: datetime) -> bool:
    if pending_tool_calls_at(session, captured_at):
        return True

    rollout_path = Path(session.rollout_path)
    if not rollout_path.exists() or not rollout_path.is_file():
        return False
    try:
        lines = rollout_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return False

    for raw_line in reversed(lines):
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_at = parse_dt(str(event.get("timestamp") or ""))
        if event_at is not None and event_at > captured_at:
            continue
        kind = _session_event_kind(event)
        if kind == "task_complete":
            return False
        if kind == "active":
            return True
    captured_ts = int(captured_at.timestamp())
    if session.updated_at > 0 and captured_ts - session.updated_at <= RECENT_SESSION_ACTIVITY_GRACE_SECONDS:
        return True
    return False


def interrupted_session_record(session: InterruptedSession, captured_at: datetime) -> dict[str, Any]:
    record = session.__dict__.copy()
    pending_tools = pending_tool_calls_at(session, captured_at)
    restartable_tools, skipped_commands = _filter_restartable_tools(pending_tools)
    record["interrupted_tools"] = restartable_tools
    record["interrupted_tool_count"] = len(restartable_tools)
    record["ignored_interrupted_command_count"] = skipped_commands
    record["raw_interrupted_tool_count"] = len(pending_tools)
    return record


def interrupted_session_snapshot_path(session_recovery_dir: Path) -> Path:
    ts = datetime.now().strftime(BACKUP_TIMESTAMP_FMT)
    return session_recovery_dir / f"interrupted-sessions-{ts}.json"


def capture_interrupted_sessions(
    *,
    codex_state_db: Path,
    codex_logs_db: Path,
    session_recovery_dir: Path,
    max_age_seconds: int,
    max_count: int,
    events_path: Path | None,
) -> dict[str, Any]:
    captured_at = now_local()
    recent_sessions = read_recent_desktop_sessions(
        codex_state_db=codex_state_db,
        codex_logs_db=codex_logs_db,
        max_age_seconds=max_age_seconds,
        max_count=max_count,
    )
    sessions = [
        session
        for session in recent_sessions
        if session_was_in_progress_at(session, captured_at)
    ]
    snapshot = {
        "captured_at": captured_at.isoformat(),
        "state_db": str(codex_state_db),
        "logs_db": str(codex_logs_db),
        "max_age_seconds": max_age_seconds,
        "max_count": max_count,
        "recent_candidates": len(recent_sessions),
        "filtered_terminal_sessions": max(0, len(recent_sessions) - len(sessions)),
        "sessions": [interrupted_session_record(session, captured_at) for session in sessions],
    }
    path = interrupted_session_snapshot_path(session_recovery_dir)
    write_json(path, snapshot)
    snapshot["path"] = str(path)
    if events_path is not None:
        append_event(
            events_path,
            "interrupted_sessions_captured",
            snapshot_path=str(path),
            session_count=len(sessions),
            recent_candidates=len(recent_sessions),
            filtered_terminal_sessions=max(0, len(recent_sessions) - len(sessions)),
            session_ids=[session.id for session in sessions],
        )
    return snapshot


def resume_interrupted_sessions(
    snapshot: dict[str, Any],
    *,
    prompt: str,
    session_recovery_dir: Path,
    events_path: Path | None,
) -> list[dict[str, Any]]:
    codex_bin = shutil_lib.which("codex")
    sessions = snapshot.get("sessions", [])
    if not codex_bin or not isinstance(sessions, list):
        if events_path is not None:
            append_event(
                events_path,
                "interrupted_sessions_resume_failed",
                reason="codex_cli_not_found" if not codex_bin else "invalid_snapshot",
                snapshot_path=snapshot.get("path"),
            )
        return []

    pending_sessions = [session for session in sessions if isinstance(session, dict)]
    session_recovery_dir.mkdir(parents=True, exist_ok=True)
    all_started: list[dict[str, Any]] = []
    inactive_session_ids: list[str] = []

    for attempt in range(1, RESUME_RETRY_ATTEMPTS + 1):
        if not pending_sessions:
            break
        attempt_prompt = prompt if attempt == 1 else DEFAULT_INTERRUPTED_SESSION_RETRY_PROMPT
        started: list[dict[str, Any]] = []
        started_processes: list[tuple[dict[str, Any], subprocess.Popen[Any], int]] = []

        for raw_session in pending_sessions:
            session_id = str(raw_session.get("id") or "")
            if not session_id:
                continue
            cwd = Path(str(raw_session.get("cwd") or Path.home()))
            if not cwd.exists() or not cwd.is_dir():
                cwd = Path.home()
            log_path = session_recovery_dir / f"{session_id}.resume.log"
            log_offset = log_path.stat().st_size if log_path.exists() else 0
            session_prompt = resume_prompt_for_session(attempt_prompt, raw_session)
            command = [
                codex_bin,
                "exec",
                "resume",
                "--skip-git-repo-check",
                session_id,
                session_prompt,
            ]
            with log_path.open("ab") as log_handle:
                process = subprocess.Popen(
                    command,
                    cwd=str(cwd),
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            record = {
                "session_id": session_id,
                "title": raw_session.get("title"),
                "cwd": str(cwd),
                "pid": process.pid,
                "log_path": str(log_path),
                "rollout_path": raw_session.get("rollout_path"),
                "resume_started_at": now_local().isoformat(),
                "interrupted_tool_count": raw_session.get("interrupted_tool_count") or 0,
                "attempt": attempt,
            }
            started.append(record)
            started_processes.append((record, process, log_offset))

        all_started.extend(started)
        verified_sessions: list[dict[str, Any]] = []
        if events_path is not None:
            append_event(
                events_path,
                "interrupted_sessions_resume_started",
                snapshot_path=snapshot.get("path"),
                prompt=attempt_prompt,
                attempt=attempt,
                session_count=len(started),
                sessions=started,
            )
            verified_sessions = verify_resumed_sessions_started(
                started_processes,
                events_path=events_path,
                attempt=attempt,
            )

        inactive_session_ids = [
            str(session.get("session_id") or "")
            for session in verified_sessions
            if not session.get("activity_detected")
        ]
        if not inactive_session_ids:
            break
        if events_path is not None and attempt < RESUME_RETRY_ATTEMPTS:
            append_event(
                events_path,
                "interrupted_sessions_resume_retry_scheduled",
                snapshot_path=snapshot.get("path"),
                next_attempt=attempt + 1,
                session_count=len(inactive_session_ids),
                session_ids=inactive_session_ids,
            )
        inactive_session_id_set = set(inactive_session_ids)
        pending_sessions = [
            session
            for session in pending_sessions
            if str(session.get("id") or "") in inactive_session_id_set
        ]

    if events_path is not None and inactive_session_ids:
        append_event(
            events_path,
            "interrupted_sessions_resume_still_inactive",
            snapshot_path=snapshot.get("path"),
            session_count=len(inactive_session_ids),
            session_ids=inactive_session_ids,
        )
    return all_started


def _read_log_since(path: Path, offset: int, limit: int = 20000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(max(offset, 0))
            data = handle.read(limit)
    except OSError:
        return ""
    return data.decode("utf-8", "replace")


def _resume_log_has_activity(text: str) -> bool:
    markers = (
        "\ncodex\n",
        "\nexec\n",
        "\napply patch\n",
        "\ntokens used\n",
        "task_complete",
        "function_call",
    )
    return any(marker in text for marker in markers)


def _rollout_has_activity_since(path: Path, started_at: datetime) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return False
    for raw in reversed(lines):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_at = parse_dt(str(event.get("timestamp") or ""))
        if event_at is None:
            continue
        if event_at < started_at:
            break
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        event_type = event.get("type")
        payload_type = payload.get("type")
        if event_type == "event_msg" and payload_type in {"task_started", "agent_message", "task_complete", "exec_command_end"}:
            return True
        if event_type == "response_item" and payload_type in {"function_call", "custom_tool_call", "reasoning"}:
            return True
    return False


def verify_resumed_sessions_started(
    started_processes: list[tuple[dict[str, Any], subprocess.Popen[Any], int]],
    *,
    events_path: Path,
    attempt: int = 1,
) -> list[dict[str, Any]]:
    if not started_processes:
        return []
    time.sleep(RESUME_VERIFY_DELAY_SECONDS)
    sessions: list[dict[str, Any]] = []
    for record, process, log_offset in started_processes:
        log_path = Path(str(record.get("log_path") or ""))
        rollout_path = Path(str(record.get("rollout_path") or ""))
        started_at = parse_dt(str(record.get("resume_started_at") or "")) or now_local()
        text = _read_log_since(log_path, log_offset)
        activity_detected = _rollout_has_activity_since(rollout_path, started_at) or _resume_log_has_activity(text)
        sessions.append(
            {
                "session_id": record.get("session_id"),
                "pid": record.get("pid"),
                "log_path": record.get("log_path"),
                "rollout_path": record.get("rollout_path"),
                "process_status": "running" if process.poll() is None else f"exited:{process.returncode}",
                "activity_detected": activity_detected,
            }
        )
    append_event(
        events_path,
        "interrupted_sessions_resume_verified",
        attempt=attempt,
        session_count=len(sessions),
        active_count=sum(1 for session in sessions if session["activity_detected"]),
        sessions=sessions,
    )
    return sessions


def determine_rotation_trigger(
    usage: RemoteUsageSnapshot | None,
    *,
    primary_used_percent: float | None,
    primary_reset_at: datetime | None,
    secondary_used_percent: float | None,
    secondary_reset_at: datetime | None,
    primary_threshold: float,
    secondary_threshold: float,
) -> tuple[str | None, datetime | None]:
    if usage is not None and (usage.limit_reached is True or usage.allowed is False):
        if (
            secondary_used_percent is not None
            and secondary_used_percent >= secondary_threshold
            and secondary_reset_at is not None
        ):
            return "weekly_limit", secondary_reset_at
        if (
            primary_used_percent is not None
            and primary_used_percent >= primary_threshold
            and primary_reset_at is not None
        ):
            return "primary_5h_limit", primary_reset_at
        if secondary_reset_at is not None:
            return "weekly_limit", secondary_reset_at
        if primary_reset_at is not None:
            return "primary_5h_limit", primary_reset_at

    if (
        secondary_used_percent is not None
        and secondary_used_percent >= secondary_threshold
        and secondary_reset_at is not None
    ):
        return "weekly_limit", secondary_reset_at
    if (
        primary_used_percent is not None
        and primary_used_percent >= primary_threshold
        and primary_reset_at is not None
    ):
        return "primary_5h_limit", primary_reset_at
    return None, None


def resume_prompt_for_session(base_prompt: str, raw_session: dict[str, Any]) -> str:
    return "\n".join(
        [
            base_prompt,
            "",
            "注意：你是被 Codex Desktop 重启打断的原会话。",
            "请基于本会话已有上下文，继续执行重启前正在进行的任务。",
            "先给用户一条非常简短的可见反馈，说明你正从哪个中断点继续、接下来立刻做什么；然后马上继续实际执行。",
            "不要只确认收到，也不要停下来等待用户；发完这条简短反馈后，请立即检查中断点并继续实际执行，直到任务完成或遇到明确阻塞。",
            "不要把任务交给恢复器或其他会话接手；如有工具、终端命令或后台任务被打断，由你在本会话上下文中自行判断并继续。",
        ]
    ).strip()


def record_session_only_recovery(snapshot: dict[str, Any], events_path: Path | None) -> None:
    if events_path is None:
        return
    sessions = snapshot.get("sessions", [])
    append_event(
        events_path,
        "interrupted_terminal_commands_not_restarted",
        snapshot_path=snapshot.get("path"),
        reason="session_owner_resume_only",
        session_count=len(sessions) if isinstance(sessions, list) else 0,
    )


def restart_codex_app(
    app_path: Path,
    hard: bool = True,
    wait_seconds: float = 2.0,
    *,
    resume_interrupted: bool = True,
    codex_state_db: Path = DEFAULT_CODEX_STATE_DB,
    codex_logs_db: Path = DEFAULT_CODEX_LOGS_DB,
    session_recovery_dir: Path = DEFAULT_SESSION_RECOVERY_DIR,
    session_window_seconds: int = DEFAULT_INTERRUPTED_SESSION_WINDOW_SECONDS,
    session_max_count: int = DEFAULT_INTERRUPTED_SESSION_MAX_COUNT,
    resume_prompt: str = DEFAULT_INTERRUPTED_SESSION_PROMPT,
    events_path: Path | None = None,
) -> bool:
    if not is_macos():
        print(
            "restart-after-switch requested, but automatic Codex app restart is only supported on macOS; "
            "restart Codex manually on this platform.",
            file=sys.stderr,
        )
        return False

    interrupted_snapshot = None
    if resume_interrupted:
        interrupted_snapshot = capture_interrupted_sessions(
            codex_state_db=codex_state_db,
            codex_logs_db=codex_logs_db,
            session_recovery_dir=session_recovery_dir,
            max_age_seconds=session_window_seconds,
            max_count=session_max_count,
            events_path=events_path,
        )

    stop_codex_app(graceful_first=not hard, wait_seconds=wait_seconds)

    # Force a fresh app instance after we have confirmed the old one is gone.
    completed = run_command(["open", "-n", "-a", str(app_path)])
    if completed.returncode != 0:
        raise SystemExit(f"failed to open Codex app:\n{completed.stderr or completed.stdout}")
    if not wait_for_codex_state(running=True, timeout_seconds=15.0):
        raise SystemExit("Codex relaunch was requested, but the app did not come back up in time")
    time.sleep(1.5)
    if not codex_process_running():
        raise SystemExit("Codex briefly launched and then exited during restart verification")
    if interrupted_snapshot is not None and interrupted_snapshot.get("sessions"):
        record_session_only_recovery(interrupted_snapshot, events_path)
        resume_interrupted_sessions(
            interrupted_snapshot,
            prompt=resume_prompt,
            session_recovery_dir=session_recovery_dir,
            events_path=events_path,
        )
    return True


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
    resume_interrupted_sessions: bool,
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
        + (["--refresh-usage"] if refresh_usage else [])
        + ([] if resume_interrupted_sessions else ["--no-resume-interrupted-sessions"]),
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
    if not is_macos():
        raise SystemExit("launchd is only available on macOS; use systemd-install on Linux")
    run_command(["launchctl", "bootout", launchctl_target(label)])


def launchctl_bootstrap(label: str, plist_path: Path) -> None:
    if not is_macos():
        raise SystemExit("launchd is only available on macOS; use systemd-install on Linux")
    last_output = ""
    for attempt in range(3):
        completed = run_command(["launchctl", "bootstrap", launchctl_domain(), str(plist_path)])
        if completed.returncode == 0:
            return
        last_output = completed.stderr or completed.stdout
        if "Bootstrap failed: 5" not in last_output:
            break
        time.sleep(1.0 + attempt)
    raise SystemExit(f"failed to bootstrap {label}:\n{last_output}")


def launchctl_kickstart(label: str) -> None:
    if not is_macos():
        raise SystemExit("launchd is only available on macOS; use systemd-install on Linux")
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
    if not is_macos():
        return status
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


def systemd_service_path(service_name: str) -> Path:
    return DEFAULT_SYSTEMD_DIR / service_name


def systemd_unit_name(service_name: str) -> str:
    return service_name if service_name.endswith(".service") else f"{service_name}.service"


def systemctl_user(args: list[str]) -> subprocess.CompletedProcess[str]:
    return run_command(["systemctl", "--user", *args])


def write_systemd_service(
    *,
    service_name: str,
    command_path: str,
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
    resume_interrupted_sessions: bool,
    stdout_path: Path,
    stderr_path: Path,
) -> Path:
    unit = systemd_unit_name(service_name)
    service_path = systemd_service_path(unit)
    service_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
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
    if restart_after_switch:
        command.append("--restart-after-switch")
    if refresh_usage:
        command.append("--refresh-usage")
    if not resume_interrupted_sessions:
        command.append("--no-resume-interrupted-sessions")

    service_path.write_text(
        "\n".join(
            [
                "[Unit]",
                "Description=Codex Auth Pool account rotation daemon",
                "After=network-online.target",
                "",
                "[Service]",
                "Type=simple",
                f"ExecStart={' '.join(shlex.quote(part) for part in command)}",
                "Restart=always",
                "RestartSec=10",
                f"StandardOutput=append:{stdout_path}",
                f"StandardError=append:{stderr_path}",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]
        )
    )
    return service_path


def systemd_status(service_name: str) -> dict[str, Any]:
    unit = systemd_unit_name(service_name)
    service_path = systemd_service_path(unit)
    status = {
        "service": unit,
        "service_path": str(service_path),
        "installed": service_path.exists(),
        "active": False,
        "state": None,
        "pid": None,
    }
    if not is_linux() or not shutil_lib.which("systemctl"):
        return status
    completed = systemctl_user(["show", unit, "--property=ActiveState,SubState,MainPID", "--no-page"])
    if completed.returncode != 0:
        return status
    for raw_line in completed.stdout.splitlines():
        key, _, value = raw_line.partition("=")
        if key == "ActiveState":
            status["active"] = value == "active"
            status["state"] = value
        elif key == "SubState" and status["state"]:
            status["state"] = f"{status['state']}/{value}"
        elif key == "MainPID" and value and value != "0":
            status["pid"] = value
    return status


def background_service_status() -> dict[str, Any]:
    if is_macos():
        status = launchctl_status(DEFAULT_LAUNCHD_LABEL)
        return {
            "kind": "launchd",
            "installed": status["installed"],
            "running": bool(status["loaded"]),
            "state": status["state"],
            "pid": status["pid"],
        }
    if is_linux():
        status = systemd_status(DEFAULT_SYSTEMD_SERVICE)
        return {
            "kind": "systemd",
            "installed": status["installed"],
            "running": bool(status["active"]),
            "state": status["state"],
            "pid": status["pid"],
        }
    return {"kind": platform.system(), "installed": False, "running": False, "state": None, "pid": None}


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


def usage_error_checked_at_for_profile(path: Path) -> datetime | None:
    return parse_dt(read_profile_metadata(path).get("usage_error_checked_at"))


def initial_usage_refresh_due(path: Path, max_age_minutes: int) -> bool:
    if usage_checked_at_for_profile(path) is not None:
        return False
    failed_at = usage_error_checked_at_for_profile(path)
    if failed_at is None:
        return True
    return now_local() - failed_at > timedelta(minutes=max_age_minutes)


def best_usage_profile_paths(source_dir: Path, managed_dir: Path) -> list[Path]:
    deduped: dict[str, Path] = {}
    for path in discover_all_profiles(source_dir, managed_dir):
        summary = summarize_profile(path)
        existing = deduped.get(summary.account_id)
        if existing is None:
            deduped[summary.account_id] = path
            continue
        existing_summary = summarize_profile(existing)
        better = (
            source_rank(summary.source_kind),
            parse_dt(summary.last_refresh) or datetime(1, 1, 1, tzinfo=timezone.utc),
            str(path),
        ) < (
            source_rank(existing_summary.source_kind),
            parse_dt(existing_summary.last_refresh) or datetime(1, 1, 1, tzinfo=timezone.utc),
            str(existing),
        )
        if better:
            deduped[summary.account_id] = path
    return list(deduped.values())


def auto_discover_new_profiles(
    source_dir: Path,
    managed_dir: Path,
    events_path: Path,
    *,
    refresh_missing_usage: bool,
    max_age_minutes: int,
    max_initial_usage_refreshes: int = AUTO_DISCOVERY_MAX_INITIAL_USAGE_REFRESHES,
) -> dict[str, Any]:
    synced = sync_cliproxy_into_managed(source_dir, managed_dir)
    refreshed: list[str] = []
    failed: list[dict[str, str]] = []

    if refresh_missing_usage:
        candidates = [
            path
            for path in best_usage_profile_paths(source_dir, managed_dir)
            if initial_usage_refresh_due(path, max_age_minutes)
        ]
        for path in candidates[:max_initial_usage_refreshes]:
            try:
                refresh_profile_usage(path)
                refreshed.append(str(path))
            except SystemExit as exc:
                failed.append({"profile": str(path), "error": str(exc)[:300]})
                update_profile_metadata(
                    path,
                    usage_error_checked_at=now_local().isoformat(),
                    usage_error=str(exc)[:300],
                )

    if synced or refreshed or failed:
        append_event(
            events_path,
            "auto_discovery",
            synced_count=len(synced),
            refreshed_count=len(refreshed),
            failed_count=len(failed),
            synced_profiles=[str(path) for path in synced],
            refreshed_profiles=refreshed,
            failed_profiles=failed,
        )

    return {
        "synced": synced,
        "refreshed": refreshed,
        "failed": failed,
    }


def profile_path_for_account_usage(source_dir: Path, managed_dir: Path, account_id: str) -> Path | None:
    matches: list[ProfileSummary] = []
    for path in discover_all_profiles(source_dir, managed_dir):
        summary = summarize_profile(path)
        if summary.account_id == account_id:
            matches.append(summary)
    if not matches:
        return None
    matches.sort(
        key=lambda summary: (
            source_rank(summary.source_kind),
            parse_dt(summary.last_refresh) or datetime(1, 1, 1, tzinfo=timezone.utc),
            str(summary.path),
        )
    )
    return matches[0].path


def refresh_current_account_usage(args: argparse.Namespace, current_account: str) -> bool:
    path = profile_path_for_account_usage(args.source_dir, args.managed_dir, current_account)
    if path is None:
        return False
    try:
        refresh_profile_usage(path)
        append_event(
            args.events_path,
            "refresh_current_usage",
            profile=str(path),
            account_id=current_account,
            forced=True,
        )
        if not getattr(args, "daemon_quiet", False):
            print(f"refreshed current account usage: {path.name}")
        return True
    except SystemExit as exc:
        update_profile_metadata(
            path,
            usage_error_checked_at=now_local().isoformat(),
            usage_error=str(exc)[:300],
        )
        append_event(
            args.events_path,
            "refresh_current_usage_failed",
            profile=str(path),
            account_id=current_account,
            error=str(exc)[:300],
        )
        if not getattr(args, "daemon_quiet", False):
            print(f"current account usage refresh failed: {exc}")
        return False


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
        "usage_error_checked_at": None,
        "usage_error": None,
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
            if not isinstance(event, dict):
                continue
            payload = event.get("payload", {})
            if not isinstance(payload, dict):
                continue
            if payload.get("type") != "token_count":
                continue
            rate_limits = payload.get("rate_limits", {})
            if not isinstance(rate_limits, dict):
                continue
            primary = rate_limits.get("primary", {})
            secondary = rate_limits.get("secondary", {})
            if not isinstance(primary, dict):
                primary = {}
            if not isinstance(secondary, dict):
                secondary = {}
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
    discovery = auto_discover_new_profiles(
        args.source_dir,
        args.managed_dir,
        args.events_path,
        refresh_missing_usage=True,
        max_age_minutes=DEFAULT_USAGE_MAX_AGE_MINUTES,
    )
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
    if discovery["synced"] or discovery["refreshed"] or discovery["failed"]:
        print("")
        print("Auto Discovery")
        print(f"  imported new cliproxy accounts: {len(discovery['synced'])}")
        print(f"  refreshed new usage snapshots: {len(discovery['refreshed'])}")
        print(f"  failed new usage refreshes: {len(discovery['failed'])}")
    print("")
    print("Last Switch")
    last_apply = state.get("last_apply") if isinstance(state.get("last_apply"), dict) else None
    if last_apply is None:
        last_apply = read_recent_event_of_type(args.events_path, "apply_profile")
    if last_apply is None:
        print("  none")
    else:
        print(f"  account: {last_apply.get('email') or last_apply.get('account_id') or '-'}")
        print(f"  time: {last_apply.get('timestamp') or '-'}")
        print(f"  source: {last_apply.get('apply_source') or 'legacy/manual'}")
        print(f"  reason: {last_apply.get('rotation_reason') or '-'}")
        print(f"  trigger: {last_apply.get('rotation_trigger_source') or '-'}")
        restarted = last_apply.get("restart_performed")
        if restarted is None:
            restarted = last_apply.get("restart_after_switch")
        print(f"  restarted: {'yes' if restarted else 'no'}")
    print("")
    print("Session Recovery")
    capture = recent_interrupted_capture_summary(args.events_path)
    session_only_recovery = recent_session_only_recovery_summary(args.events_path)
    terminal_recovery = recent_restarted_terminal_summary(args.events_path)
    recovery = recent_resumed_sessions_summary(args.events_path)
    resume_verification = recent_resume_verification_summary(args.events_path)
    if capture is None and session_only_recovery is None and terminal_recovery is None and recovery is None and resume_verification is None:
        print("  none")
    else:
        if capture is not None:
            print(f"  captured at: {capture['timestamp'] or '-'}")
            print(f"  recent candidates: {capture['recent_candidates'] if capture['recent_candidates'] is not None else '-'}")
            print(f"  filtered completed: {capture['filtered_terminal_sessions'] if capture['filtered_terminal_sessions'] is not None else '-'}")
            print(f"  queued for resume: {capture['session_count']}")
        if session_only_recovery is not None:
            print(f"  mode: resume original Codex sessions only")
            print(f"  terminal commands restarted: 0")
        elif terminal_recovery is not None:
            print(f"  terminal commands restarted at: {terminal_recovery['timestamp'] or '-'}")
            print(f"  terminal commands restarted: {terminal_recovery['command_count']}")
        if recovery is not None:
            print(f"  resumed at: {recovery['timestamp'] or '-'}")
            print(f"  resumed sessions: {recovery['session_count']}")
            print(f"  examples: {', '.join(recovery['titles']) if recovery['titles'] else '-'}")
        if resume_verification is not None:
            print(f"  resume verified at: {resume_verification['timestamp'] or '-'}")
            print(f"  sessions with activity: {resume_verification['active_count']}/{resume_verification['session_count']}")
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
        active_cooldown = item["cooldown_until"] is not None and item["cooldown_until"] > now_local()
        if active_cooldown:
            flags.append(f"cooldown_until={item['cooldown_until'].isoformat()}")
        if active_cooldown and item["cooldown_reason"]:
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
    auto_discover_new_profiles(
        args.source_dir,
        args.managed_dir,
        args.events_path,
        refresh_missing_usage=True,
        max_age_minutes=DEFAULT_USAGE_MAX_AGE_MINUTES,
    )
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
    with exclusive_lock(DEFAULT_APPLY_LOCK_PATH, blocking=True) as acquired:
        if not acquired:
            print("another auth apply is running; skipping")
            return 0
        return cmd_apply_locked(args)


def cmd_apply_locked(args: argparse.Namespace) -> int:
    apply_source = getattr(args, "apply_source", "manual")
    rotation_reason = getattr(args, "rotation_reason", None)
    rotation_trigger_source = getattr(args, "rotation_trigger_source", None)
    if apply_source == "auto_rotation" and (not rotation_reason or not rotation_trigger_source):
        raise SystemExit("refusing automatic apply without an explicit quota rotation trigger")

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
    state = load_state(args.state_path) if getattr(args, "state_path", None) else {}
    if getattr(args, "state_path", None):
        state["current_profile"] = str(profile_path)
        if state.get("preferred_next_account_id") == normalized.get("account_id"):
            state.pop("preferred_next_account_id", None)
    print(f"applied profile {profile_path.name} to {target_path}")
    for synced in synced_targets:
        print(f"synced secondary auth file {synced}")

    restart_requested = bool(getattr(args, "restart_after_switch", False))
    restart_performed = False
    restart_skipped_reason = None
    if restart_requested:
        restart_now = now_local()
        recently_blocked, retry_after_seconds = restart_recently_blocked(state, now=restart_now)
        if apply_source == "auto_rotation" and recently_blocked:
            restart_skipped_reason = f"recent_restart_retry_after_{retry_after_seconds}s"
            print(f"restart skipped: recent automatic restart, retry after {retry_after_seconds}s")
        elif restart_codex_app(
            Path(args.app_path),
            hard=not getattr(args, "graceful_restart", False),
            resume_interrupted=not getattr(args, "no_resume_interrupted_sessions", False),
            codex_state_db=getattr(args, "codex_state_db", DEFAULT_CODEX_STATE_DB),
            codex_logs_db=getattr(args, "codex_logs_db", DEFAULT_CODEX_LOGS_DB),
            session_recovery_dir=getattr(args, "session_recovery_dir", DEFAULT_SESSION_RECOVERY_DIR),
            events_path=getattr(args, "events_path", None),
        ):
            restart_performed = True
            state["last_restart_at"] = restart_now.isoformat()
            print(f"restarted Codex app {args.app_path}")

    apply_time = now_local()
    if getattr(args, "state_path", None):
        state["last_apply"] = {
            "timestamp": apply_time.isoformat(),
            "profile": str(profile_path),
            "email": normalized.get("email"),
            "account_id": normalized.get("account_id"),
            "apply_source": apply_source,
            "rotation_reason": rotation_reason,
            "rotation_trigger_source": rotation_trigger_source,
            "restart_requested": restart_requested,
            "restart_performed": restart_performed,
            "restart_skipped_reason": restart_skipped_reason,
        }
        if apply_source == "auto_rotation":
            state["last_auto_rotation_at"] = apply_time.isoformat()
        save_state(args.state_path, state)
    append_event(
        args.events_path,
        "apply_profile",
        profile=str(profile_path),
        source_kind=normalized.get("source_kind"),
        email=normalized.get("email"),
        account_id=normalized.get("account_id"),
        restart_after_switch=restart_requested,
        restart_performed=restart_performed,
        restart_skipped_reason=restart_skipped_reason,
        apply_source=apply_source,
        rotation_reason=rotation_reason,
        rotation_trigger_source=rotation_trigger_source,
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
    auto_discover_new_profiles(
        args.source_dir,
        args.managed_dir,
        args.events_path,
        refresh_missing_usage=True,
        max_age_minutes=getattr(args, "usage_max_age_minutes", DEFAULT_USAGE_MAX_AGE_MINUTES),
    )
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
    auto_discover_new_profiles(
        args.source_dir,
        args.managed_dir,
        args.events_path,
        refresh_missing_usage=False,
        max_age_minutes=args.max_age_minutes,
    )
    if getattr(args, "profile", None):
        profiles = [pick_profile(args.source_dir, args.managed_dir, args.profile)]
    else:
        profiles = best_usage_profile_paths(args.source_dir, args.managed_dir)
    if not profiles:
        print(f"no auth profiles found in {args.source_dir} or {args.managed_dir}")
        return 0

    refreshed = 0
    failed = 0
    quiet = bool(getattr(args, "quiet", False))
    if not quiet:
        print("Usage Refresh")
    for path in profiles:
        if not getattr(args, "force", False) and usage_is_stale(path, args.max_age_minutes) is False:
            checked_at = usage_checked_at_for_profile(path)
            if not quiet:
                print(f"  - {path.name}: skipped (fresh until {checked_at.isoformat() if checked_at else '-'})")
            continue
        try:
            meta = refresh_profile_usage(path)
            refreshed += 1
            if not quiet:
                print(
                    f"  - {path.name}: weekly={meta.get('observed_secondary_used_percent', '-')}% "
                    f"reset={meta.get('observed_secondary_reset_at', '-')}"
                )
        except SystemExit as exc:
            failed += 1
            if not quiet:
                print(f"  - {path.name}: failed ({exc})")

    append_event(
        args.events_path,
        "refresh_usage",
        refreshed_count=refreshed,
        failed_count=failed,
        profile=getattr(args, "profile", None),
        force=bool(getattr(args, "force", False)),
    )
    if quiet:
        print(f"usage refresh: refreshed={refreshed} failed={failed}")
    else:
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

    if is_macos() and Path(args.app_path).exists():
        report("OK", "Codex app", str(args.app_path))
    elif is_macos():
        issues += 1
        report("ERR", "Codex app", f"missing at {args.app_path}")
    else:
        report("OK", "Codex app", "not required on this platform")

    background = background_service_status()
    if background["installed"] and background["running"]:
        report("OK", background["kind"], f"active state={background['state'] or '-'}")
    elif background["installed"]:
        warnings += 1
        report("WARN", background["kind"], "service exists but is not running")
    else:
        warnings += 1
        report("WARN", background["kind"], "background service not installed")

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
    discovery = auto_discover_new_profiles(
        args.source_dir,
        args.managed_dir,
        args.events_path,
        refresh_missing_usage=True,
        max_age_minutes=DEFAULT_USAGE_MAX_AGE_MINUTES,
    )
    state = load_state(args.state_path)
    target_path = Path(args.target)
    current_summary = current_account_summary(target_path, args.source_dir, args.managed_dir)
    ranked = rank_profiles(args.source_dir, args.managed_dir, state, target_path)
    current_item = next((item for item in ranked if item["is_current"]), None)
    next_item = next((item for item in ranked if item["available"] and not item["is_current"]), None)
    snapshot = latest_rate_limit_snapshot(args.sessions_dir)
    background = background_service_status()
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
    print("Auto Discovery")
    if discovery["synced"] or discovery["refreshed"] or discovery["failed"]:
        print(f"  imported new cliproxy accounts: {len(discovery['synced'])}")
        print(f"  refreshed new usage snapshots: {len(discovery['refreshed'])}")
        print(f"  failed new usage refreshes: {len(discovery['failed'])}")
    else:
        print("  no new accounts found")
    print("")
    print("Last Switch")
    last_apply = state.get("last_apply") if isinstance(state.get("last_apply"), dict) else None
    if last_apply is None:
        last_apply = read_recent_event_of_type(args.events_path, "apply_profile")
    if last_apply is None:
        print("  none")
    else:
        print(f"  account: {last_apply.get('email') or last_apply.get('account_id') or '-'}")
        print(f"  time: {last_apply.get('timestamp') or '-'}")
        print(f"  source: {last_apply.get('apply_source') or 'legacy/manual'}")
        print(f"  reason: {last_apply.get('rotation_reason') or '-'}")
        print(f"  trigger: {last_apply.get('rotation_trigger_source') or '-'}")
        restarted = last_apply.get("restart_performed")
        if restarted is None:
            restarted = last_apply.get("restart_after_switch")
        print(f"  restarted: {'yes' if restarted else 'no'}")
    print("")
    print("Session Recovery")
    capture = recent_interrupted_capture_summary(args.events_path)
    session_only_recovery = recent_session_only_recovery_summary(args.events_path)
    terminal_recovery = recent_restarted_terminal_summary(args.events_path)
    recovery = recent_resumed_sessions_summary(args.events_path)
    resume_verification = recent_resume_verification_summary(args.events_path)
    if capture is None and session_only_recovery is None and terminal_recovery is None and recovery is None and resume_verification is None:
        print("  none")
    else:
        if capture is not None:
            print(f"  captured at: {capture['timestamp'] or '-'}")
            print(f"  recent candidates: {capture['recent_candidates'] if capture['recent_candidates'] is not None else '-'}")
            print(f"  filtered completed: {capture['filtered_terminal_sessions'] if capture['filtered_terminal_sessions'] is not None else '-'}")
            print(f"  queued for resume: {capture['session_count']}")
        if session_only_recovery is not None:
            print(f"  mode: resume original Codex sessions only")
            print(f"  terminal commands restarted: 0")
        elif terminal_recovery is not None:
            print(f"  terminal commands restarted at: {terminal_recovery['timestamp'] or '-'}")
            print(f"  terminal commands restarted: {terminal_recovery['command_count']}")
        if recovery is not None:
            print(f"  resumed at: {recovery['timestamp'] or '-'}")
            print(f"  resumed sessions: {recovery['session_count']}")
            print(f"  examples: {', '.join(recovery['titles']) if recovery['titles'] else '-'}")
        if resume_verification is not None:
            print(f"  resume verified at: {resume_verification['timestamp'] or '-'}")
            print(f"  sessions with activity: {resume_verification['active_count']}/{resume_verification['session_count']}")
    print("")
    print("Daemon")
    print(f"  kind: {background['kind']}")
    print(f"  installed: {'yes' if background['installed'] else 'no'}")
    print(f"  running: {'yes' if background['running'] else 'no'}")
    print(f"  state: {background['state'] or '-'}")
    print(f"  pid: {background['pid'] or '-'}")
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
    report["background_service"] = background_service_status()
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
    if getattr(args, "install_systemd", False):
        systemd_args = argparse.Namespace(**vars(args))
        systemd_args.service_name = DEFAULT_SYSTEMD_SERVICE
        systemd_args.stdout_path = DEFAULT_SYSTEMD_STDOUT
        systemd_args.stderr_path = DEFAULT_SYSTEMD_STDERR
        cmd_systemd_install(systemd_args)
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
    if getattr(args, "install_systemd", False):
        systemd_args = argparse.Namespace(**vars(args))
        systemd_args.service_name = DEFAULT_SYSTEMD_SERVICE
        systemd_args.stdout_path = DEFAULT_SYSTEMD_STDOUT
        systemd_args.stderr_path = DEFAULT_SYSTEMD_STDERR
        cmd_systemd_install(systemd_args)

    print("init complete")
    print("you can now use:")
    print("  codex-auth-pool status")
    print("  codex-auth-pool pick")
    print("  codex-auth-pool apply-best --restart-after-switch")
    if args.install_launchd:
        print("  codex-auth-pool launchd-status")
    if getattr(args, "install_systemd", False):
        print("  codex-auth-pool systemd-status")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    if not args.events_path.exists():
        print(f"no events file found at {args.events_path}")
        return 0
    lines = args.events_path.read_text(encoding="utf-8").splitlines()
    tail = lines[-args.limit :] if args.limit > 0 else lines
    for line in tail:
        if getattr(args, "raw", False):
            print(line)
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            print(line)
            continue
        event_type = event.get("event_type", "-")
        timestamp = event.get("timestamp", "-")
        subject = event.get("email") or event.get("account_id") or event.get("profile") or "-"
        details = []
        for key in (
            "apply_source",
            "rotation_reason",
            "rotation_trigger_source",
            "restart_after_switch",
            "restart_performed",
            "restart_skipped_reason",
            "reason",
            "cooldown_until",
            "retry_after_seconds",
            "synced_count",
            "refreshed_count",
            "failed_count",
            "session_count",
            "recent_candidates",
            "filtered_terminal_sessions",
            "command_count",
        ):
            value = event.get(key)
            if value not in (None, ""):
                details.append(f"{key}={value}")
        detail = f" ({', '.join(details)})" if details else ""
        print(f"{timestamp}  {event_type}  {subject}{detail}")
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
        if restart_codex_app(
            Path(args.app_path),
            hard=not getattr(args, "graceful_restart", False),
            resume_interrupted=not getattr(args, "no_resume_interrupted_sessions", False),
            codex_state_db=getattr(args, "codex_state_db", DEFAULT_CODEX_STATE_DB),
            codex_logs_db=getattr(args, "codex_logs_db", DEFAULT_CODEX_LOGS_DB),
            session_recovery_dir=getattr(args, "session_recovery_dir", DEFAULT_SESSION_RECOVERY_DIR),
            events_path=getattr(args, "events_path", None),
        ):
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
    with exclusive_lock(DEFAULT_TICK_LOCK_PATH, blocking=False) as acquired:
        if not acquired:
            print("another daemon tick is still running; skipping this tick")
            return 0
        return cmd_tick_locked(args)


def cmd_tick_locked(args: argparse.Namespace) -> int:
    auto_discover_new_profiles(
        args.source_dir,
        args.managed_dir,
        args.events_path,
        refresh_missing_usage=False,
        max_age_minutes=args.usage_max_age_minutes,
    )
    current_account = current_auth_account_id(Path(args.target))
    if current_account is None:
        print("no current Codex auth account detected")
        return 1

    if getattr(args, "refresh_usage", False):
        refresh_current_account_usage(args, current_account)

    if getattr(args, "refresh_usage", False):
        refresh_args = argparse.Namespace(
            source_dir=args.source_dir,
            managed_dir=args.managed_dir,
            profile=None,
            force=False,
            max_age_minutes=args.usage_max_age_minutes,
            events_path=args.events_path,
            quiet=getattr(args, "daemon_quiet", False),
        )
        cmd_refresh_usage(refresh_args)

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

    triggered_reason, triggered_until = determine_rotation_trigger(
        current_usage,
        primary_used_percent=trigger_primary_used,
        primary_reset_at=trigger_primary_reset,
        secondary_used_percent=trigger_secondary_used,
        secondary_reset_at=trigger_secondary_reset,
        primary_threshold=args.primary_threshold,
        secondary_threshold=args.secondary_threshold,
    )

    if triggered_reason and triggered_until:
        if getattr(args, "dry_run", False):
            print(
                f"dry run: would mark current account {current_account} on cooldown until "
                f"{triggered_until.isoformat()} ({triggered_reason}, source={trigger_source})"
            )
            return 0
        recent_rotation, retry_after_seconds = auto_rotation_recently_blocked(state, now=now_local())
        if recent_rotation:
            append_event(
                args.events_path,
                "rotation_skipped_recent",
                account_id=current_account,
                reason=triggered_reason,
                retry_after_seconds=retry_after_seconds,
                trigger_source=trigger_source,
            )
            print(f"rotation skipped: recent automatic rotation, retry after {retry_after_seconds}s")
            return 0
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
                args.apply_source = "auto_rotation"
                args.rotation_reason = triggered_reason
                args.rotation_trigger_source = trigger_source
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
    args.daemon_quiet = True
    while True:
        try:
            cmd_tick(args)
        except Exception as exc:  # pragma: no cover
            print(f"tick failed: {exc}", file=sys.stderr)
        time.sleep(args.interval_seconds)


def cmd_restart_codex(args: argparse.Namespace) -> int:
    restarted = restart_codex_app(
        Path(args.app_path),
        hard=not args.graceful_restart,
        resume_interrupted=not getattr(args, "no_resume_interrupted_sessions", False),
        codex_state_db=getattr(args, "codex_state_db", DEFAULT_CODEX_STATE_DB),
        codex_logs_db=getattr(args, "codex_logs_db", DEFAULT_CODEX_LOGS_DB),
        session_recovery_dir=getattr(args, "session_recovery_dir", DEFAULT_SESSION_RECOVERY_DIR),
        events_path=getattr(args, "events_path", None),
    )
    mode = "graceful" if args.graceful_restart else "hard"
    if restarted:
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
        resume_interrupted_sessions=not getattr(args, "no_resume_interrupted_sessions", False),
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


def cmd_systemd_install(args: argparse.Namespace) -> int:
    if not is_linux():
        raise SystemExit("systemd user services are only supported on Linux; use launchd-install on macOS")
    if not shutil_lib.which("systemctl"):
        raise SystemExit("systemctl not found; install systemd or run `codex-auth-pool daemon` manually")
    command_path = shutil_lib.which("codex-auth-pool")
    if not command_path:
        raise SystemExit("codex-auth-pool command not found in PATH; activate/install the package first")
    service_path = write_systemd_service(
        service_name=args.service_name,
        command_path=command_path,
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
        resume_interrupted_sessions=not getattr(args, "no_resume_interrupted_sessions", False),
        stdout_path=args.stdout_path,
        stderr_path=args.stderr_path,
    )
    systemctl_user(["daemon-reload"])
    unit = systemd_unit_name(args.service_name)
    completed = systemctl_user(["enable", "--now", unit])
    if completed.returncode != 0:
        raise SystemExit(f"failed to enable systemd user service {unit}:\n{completed.stderr or completed.stdout}")
    append_event(args.events_path, "systemd_install", service_name=unit, service_path=str(service_path))
    print(f"installed systemd user service at {service_path}")
    print(json.dumps(systemd_status(unit), ensure_ascii=False, indent=2))
    return 0


def cmd_systemd_start(args: argparse.Namespace) -> int:
    if not is_linux():
        raise SystemExit("systemd user services are only supported on Linux")
    unit = systemd_unit_name(args.service_name)
    completed = systemctl_user(["start", unit])
    if completed.returncode != 0:
        raise SystemExit(f"failed to start {unit}:\n{completed.stderr or completed.stdout}")
    print(json.dumps(systemd_status(unit), ensure_ascii=False, indent=2))
    return 0


def cmd_systemd_stop(args: argparse.Namespace) -> int:
    if not is_linux():
        raise SystemExit("systemd user services are only supported on Linux")
    unit = systemd_unit_name(args.service_name)
    completed = systemctl_user(["stop", unit])
    if completed.returncode != 0:
        raise SystemExit(f"failed to stop {unit}:\n{completed.stderr or completed.stdout}")
    print(json.dumps(systemd_status(unit), ensure_ascii=False, indent=2))
    return 0


def cmd_systemd_uninstall(args: argparse.Namespace) -> int:
    if not is_linux():
        raise SystemExit("systemd user services are only supported on Linux")
    unit = systemd_unit_name(args.service_name)
    systemctl_user(["disable", "--now", unit])
    service_path = systemd_service_path(unit)
    if service_path.exists():
        service_path.unlink()
    systemctl_user(["daemon-reload"])
    print(json.dumps(systemd_status(unit), ensure_ascii=False, indent=2))
    return 0


def cmd_systemd_status(args: argparse.Namespace) -> int:
    status = systemd_status(args.service_name)
    stdout_line = read_recent_log_line(DEFAULT_SYSTEMD_STDOUT)
    stderr_line = read_recent_log_line(DEFAULT_SYSTEMD_STDERR)
    stdout_time = file_mtime(DEFAULT_SYSTEMD_STDOUT)
    stderr_time = file_mtime(DEFAULT_SYSTEMD_STDERR)
    print("Systemd")
    print(f"  service: {status['service']}")
    print(f"  installed: {'yes' if status['installed'] else 'no'}")
    print(f"  active: {'yes' if status['active'] else 'no'}")
    print(f"  state: {status['state'] or '-'}")
    print(f"  pid: {status['pid'] or '-'}")
    print(f"  path: {status['service_path']}")
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
        "--session-recovery-dir",
        type=Path,
        default=DEFAULT_SESSION_RECOVERY_DIR,
        help=f"interrupted session recovery log directory (default: {DEFAULT_SESSION_RECOVERY_DIR})",
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
        "--codex-state-db",
        type=Path,
        default=DEFAULT_CODEX_STATE_DB,
        help=f"Codex Desktop state database (default: {DEFAULT_CODEX_STATE_DB})",
    )
    parser.add_argument(
        "--codex-logs-db",
        type=Path,
        default=DEFAULT_CODEX_LOGS_DB,
        help=f"Codex Desktop logs database (default: {DEFAULT_CODEX_LOGS_DB})",
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
    init_parser.add_argument(
        "--install-systemd",
        action="store_true",
        help="also install the background systemd user service on Linux",
    )
    init_parser.add_argument("--interval-seconds", type=int, default=60)
    init_parser.add_argument("--primary-threshold", type=float, default=DEFAULT_PRIMARY_THRESHOLD)
    init_parser.add_argument("--secondary-threshold", type=float, default=DEFAULT_SECONDARY_THRESHOLD)
    init_parser.add_argument("--restart-after-switch", action="store_true")
    init_parser.add_argument(
        "--no-resume-interrupted-sessions",
        action="store_true",
        help="when installing a background agent, do not auto-send '继续' after Codex restarts",
    )
    init_parser.add_argument("--usage-max-age-minutes", type=int, default=DEFAULT_USAGE_MAX_AGE_MINUTES)
    init_parser.add_argument(
        "--refresh-usage",
        action="store_true",
        default=True,
        help="refresh per-account real usage windows before each rotation check (default: enabled)",
    )
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
    restore_env_parser.add_argument(
        "--no-resume-interrupted-sessions",
        action="store_true",
        help="do not send '继续' to recently active Codex Desktop sessions after restarting",
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
        default=DEFAULT_USAGE_MAX_AGE_MINUTES,
        help="skip profiles whose usage observation is newer than this many minutes unless --force is set",
    )
    refresh_usage_parser.set_defaults(func=cmd_refresh_usage)

    setup_parser = subparsers.add_parser(
        "setup",
        help="one-shot onboarding: write config and optionally install a background agent/service",
    )
    setup_parser.add_argument("--install-launchd", action="store_true", help="also install the launchd background agent")
    setup_parser.add_argument("--install-systemd", action="store_true", help="also install the systemd user service on Linux")
    setup_parser.add_argument("--interval-seconds", type=int, default=60)
    setup_parser.add_argument("--primary-threshold", type=float, default=DEFAULT_PRIMARY_THRESHOLD)
    setup_parser.add_argument("--secondary-threshold", type=float, default=DEFAULT_SECONDARY_THRESHOLD)
    setup_parser.add_argument("--restart-after-switch", action="store_true")
    setup_parser.add_argument(
        "--no-resume-interrupted-sessions",
        action="store_true",
        help="when installing a background agent, do not auto-send '继续' after Codex restarts",
    )
    setup_parser.add_argument("--usage-max-age-minutes", type=int, default=DEFAULT_USAGE_MAX_AGE_MINUTES)
    setup_parser.add_argument(
        "--refresh-usage",
        action="store_true",
        default=True,
        help="refresh per-account real usage windows before each rotation check (default: enabled)",
    )
    setup_parser.add_argument(
        "--snapshot-env",
        action="store_true",
        help="also capture a baseline local environment snapshot during setup",
    )
    setup_parser.add_argument(
        "--snapshot-name",
        help="optional snapshot name to use with --snapshot-env",
    )
    setup_parser.add_argument("--no-notify", action="store_true", help="disable notifications for setup-triggered steps")
    setup_parser.set_defaults(func=cmd_setup)

    events_parser = subparsers.add_parser(
        "events",
        help="show recent save/import/cooldown/switch events from the auth-pool event log",
    )
    events_parser.add_argument("--limit", type=int, default=20)
    events_parser.add_argument("--raw", action="store_true", help="print raw JSONL events")
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
    apply_parser.add_argument(
        "--no-resume-interrupted-sessions",
        action="store_true",
        help="do not send '继续' to recently active Codex Desktop sessions after restarting",
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
    apply_best_parser.add_argument(
        "--no-resume-interrupted-sessions",
        action="store_true",
        help="do not send '继续' to recently active Codex Desktop sessions after restarting",
    )
    apply_best_parser.add_argument(
        "--usage-max-age-minutes",
        type=int,
        default=DEFAULT_USAGE_MAX_AGE_MINUTES,
        help="avoid retrying failed initial usage observations newer than this many minutes",
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
        "--dry-run",
        action="store_true",
        help="show whether rotation would trigger without changing state or switching accounts",
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
        "--no-resume-interrupted-sessions",
        action="store_true",
        help="do not send '继续' to recently active Codex Desktop sessions after restarting",
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
        default=DEFAULT_USAGE_MAX_AGE_MINUTES,
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
        "--no-resume-interrupted-sessions",
        action="store_true",
        help="do not send '继续' to recently active Codex Desktop sessions after restarting",
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
        default=DEFAULT_USAGE_MAX_AGE_MINUTES,
        help="consider cached per-account usage observations stale after this many minutes",
    )
    daemon_parser.set_defaults(func=cmd_daemon)

    restart_parser = subparsers.add_parser("restart-codex", help="restart Codex Desktop now")
    restart_parser.add_argument(
        "--graceful-restart",
        action="store_true",
        help="use AppleScript quit instead of hard process kill before reopening",
    )
    restart_parser.add_argument(
        "--no-resume-interrupted-sessions",
        action="store_true",
        help="do not send '继续' to recently active Codex Desktop sessions after restarting",
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
        default=DEFAULT_USAGE_MAX_AGE_MINUTES,
        help="consider cached per-account usage observations stale after this many minutes",
    )
    launchd_install_parser.add_argument(
        "--restart-after-switch",
        action="store_true",
        help="restart Codex Desktop automatically after switching auth",
    )
    launchd_install_parser.add_argument(
        "--no-resume-interrupted-sessions",
        action="store_true",
        help="do not send '继续' to recently active Codex Desktop sessions after automatic restart",
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

    systemd_install_parser = subparsers.add_parser(
        "systemd-install",
        help="install a Linux systemd user service for automatic auth-pool rotation",
    )
    systemd_install_parser.add_argument("--service-name", default=DEFAULT_SYSTEMD_SERVICE)
    systemd_install_parser.add_argument("--interval-seconds", type=int, default=60)
    systemd_install_parser.add_argument("--primary-threshold", type=float, default=DEFAULT_PRIMARY_THRESHOLD)
    systemd_install_parser.add_argument("--secondary-threshold", type=float, default=DEFAULT_SECONDARY_THRESHOLD)
    systemd_install_parser.add_argument(
        "--refresh-usage",
        action="store_true",
        default=True,
        help="refresh per-account real usage windows before each daemon rotation check (default: enabled)",
    )
    systemd_install_parser.add_argument(
        "--no-refresh-usage",
        action="store_false",
        dest="refresh_usage",
        help="skip direct per-account usage refreshes in the daemon",
    )
    systemd_install_parser.add_argument("--usage-max-age-minutes", type=int, default=DEFAULT_USAGE_MAX_AGE_MINUTES)
    systemd_install_parser.add_argument(
        "--restart-after-switch",
        action="store_true",
        help="request Codex restart after switching; currently a no-op on Linux",
    )
    systemd_install_parser.add_argument(
        "--no-resume-interrupted-sessions",
        action="store_true",
        help="do not send '继续' to recently active Codex Desktop sessions after automatic restart",
    )
    systemd_install_parser.add_argument("--stdout-path", type=Path, default=DEFAULT_SYSTEMD_STDOUT)
    systemd_install_parser.add_argument("--stderr-path", type=Path, default=DEFAULT_SYSTEMD_STDERR)
    systemd_install_parser.set_defaults(func=cmd_systemd_install)

    systemd_start_parser = subparsers.add_parser("systemd-start", help="start the systemd user service")
    systemd_start_parser.add_argument("--service-name", default=DEFAULT_SYSTEMD_SERVICE)
    systemd_start_parser.set_defaults(func=cmd_systemd_start)

    systemd_stop_parser = subparsers.add_parser("systemd-stop", help="stop the systemd user service")
    systemd_stop_parser.add_argument("--service-name", default=DEFAULT_SYSTEMD_SERVICE)
    systemd_stop_parser.set_defaults(func=cmd_systemd_stop)

    systemd_uninstall_parser = subparsers.add_parser("systemd-uninstall", help="remove the systemd user service")
    systemd_uninstall_parser.add_argument("--service-name", default=DEFAULT_SYSTEMD_SERVICE)
    systemd_uninstall_parser.set_defaults(func=cmd_systemd_uninstall)

    systemd_status_parser = subparsers.add_parser("systemd-status", help="show systemd user service status")
    systemd_status_parser.add_argument("--service-name", default=DEFAULT_SYSTEMD_SERVICE)
    systemd_status_parser.set_defaults(func=cmd_systemd_status)

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

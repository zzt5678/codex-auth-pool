# Codex Auth Pool

Multi-account rotation for Codex Desktop without giving up official ChatGPT login or `computer use`.

[中文说明](./README_CN.md)

## What It Solves

If you use Codex through the official ChatGPT login flow, you usually hit two problems:

- one account is not enough
- switching accounts manually is annoying and error-prone

`codex-auth-pool` turns that into a local account pool:

- keep multiple official `codex login` sessions
- import `cliproxyapi` auth files if you already have them
- store managed profiles as native Codex `auth.json` files
- query ChatGPT directly for real 5-hour and weekly reset windows
- rotate to the next account automatically
- keep Codex Desktop compatible with `computer use`

## Who This Is For

- Codex Desktop users on macOS
- Codex CLI users on Ubuntu/Linux who want auth rotation without Desktop-specific features
- people running more than one ChatGPT/Codex account
- users who want automatic rotation instead of copying auth files by hand
- users who want to preserve local Codex plugin and connector state across account switches

## How It Works

The tool manages global runtime state under your home directory:

- `~/.codex/` for Codex auth, plugins, and sessions
- `~/.codex-auth-pool/` for this tool's vault, state, logs, and snapshots
- `~/.cli-proxy-api/` as an optional import source

Managed profiles are stored as native Codex auth files, so you can still copy one manually into:

- `~/.codex/cache/auth.json`
- `~/.codex/auth.json`

when you need an emergency manual switch.

## Features

- Preserve multiple official `codex login` sessions so later logins do not overwrite earlier ones.
- Import existing `cliproxyapi` accounts into the same pool.
- Auto-detect newly added `cliproxyapi` Codex accounts during status checks, dashboard views, usage refreshes, picks, and rotations.
- Automatically import new `cliproxyapi` accounts into the managed vault and fetch their first real usage snapshot.
- Query `https://chatgpt.com/backend-api/wham/usage` per account to get real reset windows.
- Rank accounts using observed reset data instead of only trusting local metadata.
- Auto-cool down exhausted accounts and switch to the next available one.
- Restart Codex Desktop automatically after switching on macOS.
- Before an automatic restart, capture recently active Codex Desktop sessions; after restart, resume those interrupted sessions with `继续`.
- Background rotation switches and restarts only after a real quota threshold trigger; normal polling does not interrupt your work.
- Built-in locks and a short automatic-rotation throttle prevent repeated ticks from causing restart loops.
- Snapshot and restore local Codex plugin, config, and connector cache state.
- Run as a background `launchd` agent on macOS.
- Run as a background `systemd --user` service on Ubuntu/Linux.

## Install

### Fastest

```bash
git clone https://github.com/zzt5678/codex-auth-pool.git
cd codex-auth-pool
./install.sh
```

If you already have the background rotator installed, rerunning `./install.sh` now also reloads the existing `launchd` or `systemd --user` service so it picks up the new code immediately.

### Manual

```bash
git clone https://github.com/zzt5678/codex-auth-pool.git
cd codex-auth-pool
pipx install .
```

## Quick Start

### 1. Check your machine

```bash
codex-auth-pool check
codex-auth-pool doctor
```

### 2. Run first-time setup

macOS:

```bash
codex-auth-pool init --install-launchd
```

Ubuntu/Linux:

```bash
codex-auth-pool init --install-systemd
```

This will:

- write a starter config
- snapshot your current local Codex environment
- save your current official login
- migrate old managed profiles if needed
- import `cliproxyapi` accounts if found
- install the background rotator if requested

### 3. Open the dashboard

```bash
codex-auth-pool dashboard
```

This is the main command most people will care about. It shows:

- current account
- current 5h and weekly usage
- next account in line
- whether the reset time is `observed` or just local metadata
- whether new `cliproxyapi` accounts were auto-imported and observed
- launchd daemon health

New `cliproxyapi` Codex accounts do not require a manual `sync-cliproxy`.
The tool auto-imports and observes new accounts when you run `dashboard`, `status`, `pick`, `apply-best`, `refresh-usage`, or when the background daemon runs `tick`.

### 4. Refresh real usage windows

```bash
codex-auth-pool refresh-usage --force
```

This queries ChatGPT directly for each account and updates the cached reset data.

### 5. Check rotation safely

```bash
codex-auth-pool tick --dry-run
codex-auth-pool events --limit 10
```

`tick --dry-run` reports whether a rotation would trigger without writing cooldowns, switching accounts, or restarting Codex.
`events` prints a readable summary by default; use `codex-auth-pool events --raw` for raw JSONL.

## Interrupted Session Recovery

Background services installed with `launchd-install`, `systemd-install`, `setup --install-*`, or `init --install-*` now enable `--restart-after-switch` by default. On macOS, that means `codex-auth-pool` does a conservative recovery pass whenever automatic rotation switches accounts:

- before quitting Codex Desktop, it captures recently active Desktop sessions from `~/.codex/state_5.sqlite` and `~/.codex/logs_2.sqlite`
- after Codex Desktop comes back up, it starts `codex exec resume <session_id> 继续` for each captured session in the background
- recovery snapshots and resume logs are written under `~/.codex-auth-pool/session-recovery/`

If you only want the restart without auto-resuming interrupted sessions:

```bash
codex-auth-pool launchd-install --no-resume-interrupted-sessions
```

If you explicitly want auth switching without restarting Codex Desktop:

```bash
codex-auth-pool launchd-install --no-restart-after-switch
```

## Most Useful Commands

```bash
codex-auth-pool dashboard
codex-auth-pool status
codex-auth-pool refresh-usage --force
codex-auth-pool save-current --name my-official-1
codex-auth-pool sync-cliproxy
codex-auth-pool tick --dry-run
codex-auth-pool events --limit 10
codex-auth-pool launchd-status
codex-auth-pool systemd-status
```

## Rotation Logic

The rotator prefers accounts that are:

1. not disabled
2. not expired
3. not in cooldown
4. not currently blocked by an observed remote limit window
5. earliest observed weekly reset time
6. otherwise earliest profile `weekly_reset_at`
7. then most recent usable auth metadata

`refresh-usage` writes direct observations into profile metadata sidecars.
For managed vault profiles, the sidecar lives next to the profile as `.meta.json`.
For imported `cliproxyapi` source profiles, metadata is stored under `~/.codex-auth-pool/source-meta/` so the original `~/.cli-proxy-api/` directory stays untouched.

When the UI says:

- `reset_source: observed`

it means the value came from ChatGPT directly, not just a local guess.

## Commands

```bash
codex-auth-pool list
codex-auth-pool dashboard
codex-auth-pool status
codex-auth-pool pick
codex-auth-pool check
codex-auth-pool doctor
codex-auth-pool save-current --name my-official-1
codex-auth-pool import-auth-file ~/.codex/auth.json --name imported-official
codex-auth-pool sync-cliproxy
codex-auth-pool refresh-usage --force
codex-auth-pool tick --dry-run
codex-auth-pool events --limit 10
codex-auth-pool apply-best --restart-after-switch
codex-auth-pool tick
codex-auth-pool launchd-install --interval-seconds 60
codex-auth-pool launchd-status
codex-auth-pool systemd-install --interval-seconds 60
codex-auth-pool systemd-status
codex-auth-pool snapshot-env --name baseline
codex-auth-pool restore-env baseline --restart-codex
```

## Paths

Priority order:

1. command-line flags
2. environment variables
3. `~/.codex-auth-pool/config.json`
4. built-in defaults

Important paths:

- Config: `~/.codex-auth-pool/config.json`
- Managed profiles: `~/.codex-auth-pool/profiles/`
- State: `~/.codex-auth-pool/state.json`
- Events: `~/.codex-auth-pool/events.jsonl`
- Environment snapshots: `~/.codex-auth-pool/env-snapshots/`
- launchd logs:
  - `~/.codex-auth-pool/logs/launchd.stdout.log`
  - `~/.codex-auth-pool/logs/launchd.stderr.log`
- systemd logs:
  - `~/.codex-auth-pool/logs/systemd.stdout.log`
  - `~/.codex-auth-pool/logs/systemd.stderr.log`

## Notes

- macOS supports Codex Desktop restart after switching
- Ubuntu/Linux supports auth rotation and `systemd --user`; automatic Codex Desktop restart is a no-op there
- updates both `~/.codex/cache/auth.json` and `~/.codex/auth.json`
- keeps local plugin and connector state out of the auth rotation path
- `apply-best --restart-after-switch` is an immediate manual switch command; use `init --install-launchd` or `launchd-install` for background auto-rotation. Background services restart Codex after switches by default; pass `--no-restart-after-switch` only if you intentionally want auth changes without a Desktop restart.
- background rotation defaults to preemptive thresholds of `95%` for the 5-hour window and `98%` for the weekly window

## Ubuntu Deployment

Prerequisites:

- Python 3.10+
- `git`
- `systemd --user` if you want the background service
- existing Codex auth under `~/.codex/`, or auth files to import from `~/.cli-proxy-api/`

Recommended install:

```bash
git clone https://github.com/zzt5678/codex-auth-pool.git
cd codex-auth-pool
./install.sh
codex-auth-pool init --install-systemd
codex-auth-pool dashboard
```

If `systemctl --user` is not available in your Ubuntu environment, run the daemon manually:

```bash
codex-auth-pool daemon --interval-seconds 60
```

## Upgrading

After pulling new code, rerun:

```bash
./install.sh
```

This now reinstalls the package and attempts to reload any existing background service automatically.

## License

MIT

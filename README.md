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
- Treat `~/.codex/auth.json` and `~/.codex/cache/auth.json` as one active login state; if they drift, the daemon reconciles them before quota checks.
- Auto-cool down exhausted accounts and switch to the next available one.
- Treat an expired current auth token (`HTTP 401 token_expired`) as an unusable account and rotate away instead of trusting stale quota snapshots.
- Restart Codex Desktop automatically after switching on macOS.
- Before an automatic restart, capture recently active Codex Desktop sessions; after restart, resume those interrupted thread IDs through the Codex app-server protocol with `继续`.
- Recovery targets the original `threadId` with `thread/resume` + `turn/start` instead of creating a separate `codex exec resume` worker session.
- After automatic rotation, detect active goal threads and open macOS Terminal with `codex resume <thread_id>` so long-running CLI goal work can continue under the new auth.
- Background rotation switches and restarts only after a real quota threshold trigger; normal polling does not interrupt your work.
- Built-in locks and a short automatic-rotation throttle prevent repeated ticks from causing restart loops.
- Snapshot and restore local Codex plugin, config, and connector cache state.
- Preserve Browser Use's local Electron browser state (`Cookies`, `Local Storage`, `Session Storage`, and `Partitions/codex-browser-app`) in environment snapshots.
- During automatic account-switch restarts, restore only Browser Use's browser-login storage, not the historical `~/.codex/plugins` directory, so plugins installed after an older snapshot are not rolled back.
- Export currently available accounts into `~/.codex/ready-auths/` as native `auth.json` files for emergency manual switching.
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
- the resume model order currently used for interrupted-session recovery
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
codex-auth-pool forecast
codex-auth-pool report --no-discover
codex-auth-pool fix
codex-auth-pool events --limit 10
```

`tick --dry-run` reports whether a rotation would trigger without writing cooldowns, switching accounts, or restarting Codex.
`forecast` explains the current account, next account, quota source, daemon status, and expected switching behavior in one screen.
`report` prints the same state as JSON for automation and debugging.
`fix` is dry-run by default and only previews low-risk repairs; use `fix --apply` to synchronize auth files, clear expired cooldowns, or create missing metadata.
`events` prints a readable summary by default; use `codex-auth-pool events --raw` for raw JSONL.

## Interrupted Session Recovery

Background services installed with `launchd-install`, `systemd-install`, `setup --install-*`, or `init --install-*` now enable `--restart-after-switch` by default. On macOS, that means `codex-auth-pool` does a conservative recovery pass whenever automatic rotation switches accounts:

- soft quota triggers write a durable `pending_rotation` record while a Desktop session still appears active, then switch automatically once the session becomes idle
- hard exhaustion also waits for active work to finish; for normal top-level sessions it waits up to 10 minutes by default, switches immediately if the session becomes idle sooner, and then forces rotation after the grace window so a zero-quota account cannot deadlock the app forever
- if a running child agent / spawned thread is detected, rotation keeps waiting for that child agent to finish instead of using the 10-minute force-switch grace window
- before quitting Codex Desktop, it captures recently active Desktop sessions from `~/.codex/state_5.sqlite` and `~/.codex/logs_2.sqlite`
- after Codex Desktop comes back up, it starts a lightweight recovery helper that calls `thread/resume` and `turn/start` for each captured `threadId`
- recovery uses the original Desktop thread path only; it no longer falls back to `codex exec resume`, because that can create a separate CLI resume instead of continuing the original Desktop session
- recovery snapshots and resume logs are written under `~/.codex-auth-pool/session-recovery/`

The daemon runs independently from the currently selected Codex account. Even if the active account has reached zero quota and Codex Desktop can no longer answer, the daemon can still observe the pending rotation, replace the auth file, and restart Codex. Adjust the hard-exhaustion grace window with:

```bash
codex-auth-pool launchd-install --hard-active-grace-seconds 600
```

If you only want the restart without auto-resuming interrupted sessions:

```bash
codex-auth-pool launchd-install --no-resume-interrupted-sessions
```

If you do not want auth switches to auto-resume active CLI goal sessions:

```bash
codex-auth-pool launchd-install --no-resume-active-goals
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
codex-auth-pool export-ready-auths
codex-auth-pool events --limit 10
codex-auth-pool launchd-status
codex-auth-pool systemd-status
```

`export-ready-auths` writes all accounts that are not expired, not in cooldown, and not blocked by observed quota windows into:

```bash
~/.codex/ready-auths/
```

For an emergency manual switch, copy one `*.auth.json` to both `~/.codex/cache/auth.json` and `~/.codex/auth.json`, then fully restart Codex Desktop.

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
codex-auth-pool forecast
codex-auth-pool report --no-discover
codex-auth-pool fix
codex-auth-pool fix --apply
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
- status and dashboard show the active auth file and whether root/cache auth are in sync
- keeps local plugin and connector state out of the auth rotation path
- for Browser Use, authorize once while it works, then run `codex-auth-pool snapshot-env --name browser-use-working-$(date +%Y%m%d-%H%M%S)`; automatic switch restarts restore that snapshot before relaunching Codex
- `apply-best --restart-after-switch` is an immediate manual switch command; use `init --install-launchd` or `launchd-install` for background auto-rotation. Background services restart Codex after switches by default; pass `--no-restart-after-switch` only if you intentionally want auth changes without a Desktop restart.
- background rotation defaults to preemptive thresholds of `90%` for the 5-hour window and `97%` for the weekly window, leaving margin before the account hard-stops
- when no alternate account is available, `status`, `dashboard`, and daemon events show blocked accounts and the earliest known unblock time

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

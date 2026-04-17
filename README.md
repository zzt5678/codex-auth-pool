# Codex Auth Pool

`codex-auth-pool` is a standalone CLI for managing multiple Codex/ChatGPT
login sessions on one machine.

The source folder can live anywhere. This project is currently checked into a
workspace only because we built it here. The real runtime state is global and
lives under your home directory, mainly:

- `~/.codex/` for Codex's own auth, plugin, and session state
- `~/.codex-auth-pool/` for this tool's config, vault, snapshots, and logs
- `~/.cli-proxy-api/` if you also import `cliproxyapi` auth files

It supports two account sources:

- `cliproxyapi` auth files from `~/.cli-proxy-api`
- a managed vault of official `codex login` snapshots under `~/.codex-auth-pool/profiles`

Managed-vault profiles are stored as native Codex `auth.json` files, with
sidecar metadata in matching `.meta.json` files. That means you can manually
copy a vault profile into `~/.codex/cache/auth.json` if you ever need an
emergency manual switch.

It can:

- preserve multiple official `codex login` sessions so later logins do not overwrite them
- import existing `cliproxyapi` auth files
- query ChatGPT directly for each account's real 5-hour and weekly reset windows
- rotate accounts automatically based on Codex's 5-hour and weekly limit signals
- sync the selected auth into Codex Desktop's live auth files
- optionally restart Codex Desktop automatically after switching
- snapshot and restore local plugin, connector, and Codex config state
- run as a background `launchd` agent on macOS

## Install

### Easiest

```bash
cd tools/codex-auth-pool
./install.sh
```

### Manual

If you want to install it yourself with `pipx`:

```bash
cd tools/codex-auth-pool
pipx install .
```

### Export As A Standalone Repo

If you want to move this project out of the current workspace into its own repo:

```bash
cd tools/codex-auth-pool
./export-standalone.sh /absolute/path/to/codex-auth-pool
```

## Quick Start

### 1. Detect local paths

```bash
codex-auth-pool check
codex-auth-pool doctor
```

### 2. First-run onboarding

```bash
codex-auth-pool init --install-launchd --restart-after-switch
```

This default init flow will:

- write `config.json`
- capture a baseline local environment snapshot as `baseline`
- save the current official Codex login as `official-current` if one exists
- migrate any older non-Codex managed profiles into native Codex auth files
- sync `cliproxyapi` source accounts into the managed Codex-format vault
- optionally install the background launchd rotator when `--install-launchd` is present

### 3. Save your current official Codex login

```bash
codex-auth-pool save-current --name my-official-1
```

### 4. Check the account pool

```bash
codex-auth-pool status
codex-auth-pool pick
codex-auth-pool refresh-usage --force
```

### 5. Manual setup path

```bash
codex-auth-pool config-init
codex-auth-pool snapshot-env --name baseline
codex-auth-pool save-current --name my-official-1
codex-auth-pool setup --install-launchd --restart-after-switch
```

## Normal Commands

```bash
codex-auth-pool list
codex-auth-pool status
codex-auth-pool pick
codex-auth-pool info
codex-auth-pool check
codex-auth-pool doctor
codex-auth-pool migrate-managed
codex-auth-pool sync-cliproxy
codex-auth-pool init --install-launchd --restart-after-switch
codex-auth-pool env-status
codex-auth-pool config-init
codex-auth-pool snapshot-env --name baseline
codex-auth-pool restore-env baseline --restart-codex
codex-auth-pool events --limit 20
codex-auth-pool save-current --name my-official-1
codex-auth-pool import-auth-file ~/.codex/auth.json --name imported-official
codex-auth-pool refresh-usage --force
codex-auth-pool rate-limits
codex-auth-pool apply-best --restart-after-switch
codex-auth-pool tick
codex-auth-pool daemon --interval-seconds 60 --restart-after-switch
codex-auth-pool launchd-status
codex-auth-pool launchd-install --interval-seconds 60 --restart-after-switch
```

If you want to suppress desktop notifications for a specific command, both forms work:

```bash
codex-auth-pool --no-notify snapshot-env --name baseline
codex-auth-pool snapshot-env --name baseline --no-notify
```

## How It Chooses Accounts

- `list` shows the raw inventory and may include more than one source for the same account
- `status`, `pick`, and `apply-best` dedupe by `account_id`
- available accounts are ranked by:
  1. not disabled
  2. not expired
  3. not on cooldown
  4. earlier directly observed weekly reset time from ChatGPT when available
  5. otherwise earlier profile `weekly_reset_at`
  6. then `last_refresh`

`refresh-usage` writes direct observations into each profile's `.meta.json`.
The dashboard and status output show `reset_source=observed` when the value came
from the ChatGPT usage endpoint rather than local synthesized metadata.

## Path Resolution

Priority order:

1. command-line flags
2. environment variables
3. `~/.codex-auth-pool/config.json`
4. built-in defaults

Supported environment variables:

- `CODEX_AUTH_POOL_SOURCE_DIR`
- `CODEX_AUTH_POOL_MANAGED_DIR`
- `CODEX_AUTH_POOL_STATE_PATH`
- `CODEX_AUTH_POOL_CONFIG_PATH`
- `CODEX_AUTH_POOL_EVENTS_PATH`
- `CODEX_AUTH_POOL_ENV_SNAPSHOTS_DIR`
- `CODEX_AUTH_POOL_TARGET`
- `CODEX_AUTH_POOL_SESSIONS_DIR`
- `CODEX_AUTH_POOL_APP_PATH`

## Files

- Config: `~/.codex-auth-pool/config.json`
- Managed profiles: `~/.codex-auth-pool/profiles/`
- State: `~/.codex-auth-pool/state.json`
- Events: `~/.codex-auth-pool/events.jsonl`
- Environment snapshots: `~/.codex-auth-pool/env-snapshots/`
- launchd logs:
  - `~/.codex-auth-pool/logs/launchd.stdout.log`
  - `~/.codex-auth-pool/logs/launchd.stderr.log`

## Notes

- This tool is designed for macOS and Codex Desktop first.
- The checkout location of this repo does not decide which Codex account is active.
- It updates both `~/.codex/cache/auth.json` and `~/.codex/auth.json`.
- It reads rate-limit snapshots from `~/.codex/sessions/`.
- It can also query `https://chatgpt.com/backend-api/wham/usage` with each profile's access token to get real per-account reset windows.
- Background auto-rotation now uses preemptive defaults of `95%` for the 5-hour window and `98%` for the weekly window, so it can switch before hard exhaustion.
- Background auto-rotation refreshes per-account observed usage windows before ranking unless you disable it with `--no-refresh-usage`.
- Environment snapshots currently restore saved items conservatively: saved plugin/config/cache state is copied back, and missing items are not deleted automatically.

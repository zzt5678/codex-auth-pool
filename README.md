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
- Before background rotation or `apply-best` overwrites auth, check whether the current official login is already in the pool; if not, save it into the managed vault by `account_id`.
- Import existing `cliproxyapi` accounts into the same pool.
- Auto-detect newly added `cliproxyapi` Codex accounts during status checks, dashboard views, usage refreshes, picks, and rotations.
- Automatically import new `cliproxyapi` accounts into the managed vault and fetch their first real usage snapshot.
- Query `https://chatgpt.com/backend-api/wham/usage` per account to get real reset windows.
- Rank accounts using observed reset data instead of only trusting local metadata.
- Treat `~/.codex/auth.json` and `~/.codex/cache/auth.json` as one active login state; if they drift, the daemon reconciles them before quota checks.
- Auto-cool down exhausted accounts and switch to the next available one.
- App/Desktop automatic rotation uses an app policy: available Pro accounts rank before Plus accounts; if every Pro account is blocked or exhausted, it falls back to Plus.
- CLI goal recovery uses a separate CLI policy: Plus first, then Free; Pro is used automatically only when its weekly reset is less than 12 hours away.
- Provide `codex-plus`: manual CLI work can run under an isolated `CODEX_HOME` without overwriting the auth currently used by Codex Desktop.
- `codex-plus` shares `~/.codex` sessions, plugins, skills, and config, so `codex resume` and installed plugins do not need a second setup.
- Treat an expired current auth token (`HTTP 401 token_expired`) as an unusable account and rotate away instead of trusting stale quota snapshots.
- Treat runtime limit signals from Codex session logs (`usage_limit_exceeded`, `rate_limit_reached_type`, or repeated `rate_limits=null`) as real exhaustion even when the displayed percentage is not exactly 100%.
- Restart Codex Desktop automatically after switching on macOS.
- Before an automatic restart, capture recently active Codex Desktop sessions; after restart, resume those interrupted thread IDs through the Codex app-server protocol with `继续`.
- Recovery targets the original `threadId` with `thread/resume` + `turn/start` instead of creating a separate `codex exec resume` worker session.
- After automatic CLI auth rotation, detect active goal threads and open macOS Terminal with `codex resume <thread_id>` so long-running CLI goal work can continue under the selected CLI auth.
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

### 2.1 Run CLI Work With Isolated Auth

The installer also provides `codex-plus`:

```bash
codex-plus
codex-plus resume <thread_id>
codex-plus --version
```

Before launching official `codex`, it selects the best currently available CLI account using `Plus -> Free -> Pro` and writes auth only into `~/.codex-auth-pool/cli-plus-home` via `CODEX_HOME`. It does not overwrite the Desktop auth files at `~/.codex/auth.json` or `~/.codex/cache/auth.json`.

To prepare the isolated home without launching CLI:

```bash
codex-auth-pool cli-prepare
```

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

If you temporarily sign in to a new account with official `codex login`, background rotation and `apply-best` automatically save that current login into the pool before replacing auth. The guard deduplicates by `account_id`, so the same account is not saved twice.

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

### 6. Run Local Tests

```bash
python -m unittest discover -s tests
```

The test suite covers the core rotation threshold rules, runtime quota signals, Browser Use active-session protection, temporary candidate cooldown after usage-refresh failures, CLI goal blocker classification, and `codex-plus` isolated-home behavior.

## Token Usage And Cost Estimate

`token-usage` scans local Codex rollout logs and summarizes token consumption by account, model, thread, or account/model pair:

```bash
codex-auth-pool token-usage
codex-auth-pool token-usage --by model --since 2026-05-01
codex-auth-pool token-usage --by thread --limit 20
codex-auth-pool token-usage --json
```

The command reports input tokens, cached input tokens, uncached input tokens, output tokens, reasoning output tokens, estimated API-standard USD cost, and estimated Codex credits. API cost uses OpenAI API standard pricing; Codex credits use the OpenAI Codex token-based rate card. This is a local estimate from logs, not an official ChatGPT Plus bill.

## Interrupted Session Recovery

Background services installed with `launchd-install`, `systemd-install`, `setup --install-*`, or `init --install-*` now enable `--restart-after-switch` by default. On macOS, that means `codex-auth-pool` does a conservative recovery pass whenever automatic rotation switches accounts:

- soft quota triggers write a durable `pending_rotation` record while a Desktop session still appears active, then switch automatically once the session becomes idle
- hard exhaustion also waits for active Desktop work to finish; by default there is no force-switch countdown for active Desktop sessions, so account switching happens after the active session becomes idle
- if a running child agent / spawned thread is detected, rotation keeps waiting for that child agent to finish before switching
- before quitting Codex Desktop, it captures recently active Desktop sessions from `~/.codex/state_5.sqlite` and `~/.codex/logs_2.sqlite`
- active goal threads do not block Desktop-session rotation; they use the separate `codex resume <thread_id>` recovery path only after the goal itself hits a quota/auth blocker
- active goal recovery uses the isolated CLI policy: Plus first, then Free; Pro is an emergency fallback only when its weekly reset is less than 12 hours away
- if a Plus/Free CLI account becomes available while a goal is running on Pro, the daemon forces the goal back through `codex-plus resume` so Pro quota is not drained unnecessarily
- goal recovery checks rollout progress first; recent progress defers recovery, and only explicit quota/auth errors that occur after the latest progress trigger `codex resume`; stale rollout silence alone will not start a duplicate long-running goal
- after a goal resume starts successfully, it terminates only older `codex resume` process trees for the same `thread_id`, so the old quota-blocked terminal task stops without touching unrelated terminals or Desktop sessions
- after Codex Desktop comes back up, it starts a lightweight recovery helper that calls `thread/resume` and `turn/start` for each captured `threadId`
- recovery uses the original Desktop thread path only; it no longer falls back to `codex exec resume`, because that can create a separate CLI resume instead of continuing the original Desktop session
- recovery snapshots and resume logs are written under `~/.codex-auth-pool/session-recovery/`

The daemon runs independently from the currently selected Codex account. Even if the active account has reached zero quota and Codex Desktop can no longer answer, the daemon can still observe the pending rotation, replace the auth file, and restart Codex after active Desktop work is idle. The default is no force-switch countdown:

```bash
codex-auth-pool launchd-install --hard-active-grace-seconds 0
```

If you explicitly want a force-switch countdown, replace `0` with the number of seconds.

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

## API Sessions Under ChatGPT Login

Sessions created while using `cliproxyapi` or another API provider may not open cleanly after switching back to official ChatGPT login. Preview locally stored API-provider sessions first:

```bash
codex-auth-pool sessions-compat
```

Convert one chosen thread:

```bash
codex-auth-pool sessions-compat --apply --thread-id <thread_id>
```

Convert every matched API-provider thread:

```bash
codex-auth-pool sessions-compat --apply --all
```

This only updates the local `model_provider` index in `~/.codex/state_5.sqlite` to `openai`; it does not rewrite rollout files. The command backs up the SQLite database before applying changes.

## Most Useful Commands

```bash
codex-auth-pool dashboard
codex-auth-pool status
codex-auth-pool refresh-usage --force
codex-auth-pool save-current --name my-official-1
codex-auth-pool sync-cliproxy
codex-auth-pool tick --dry-run
codex-auth-pool export-ready-auths
codex-auth-pool token-usage --by account
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
5. allowed by the current policy
6. for App/Desktop automatic rotation: Pro before Plus before Free/unknown
7. for CLI goal recovery and `codex-plus`: Plus before Free; Pro only if its weekly reset is within 12 hours
8. earliest observed weekly reset time
9. otherwise earliest profile `weekly_reset_at`
10. then most recent usable auth metadata

This policy does not proactively restart Codex just because a Pro account appears. It only changes which account is selected when an existing quota/auth trigger already requires a switch, preserving the existing no-surprise restart behavior. Manual `apply-best` uses the App/Desktop policy by default; use `codex-auth-pool apply-best --account-policy cli` if you explicitly want the CLI order `Plus -> Free -> near-reset Pro`. For ordinary long-running CLI work, prefer `codex-plus`; it does not change the active Desktop auth.

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
codex-auth-pool launchd-install --interval-seconds 600
codex-auth-pool launchd-status
codex-auth-pool systemd-install --interval-seconds 600
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
- background rotation does not preemptively switch by percentage by default: both the 5-hour and weekly thresholds are `100%`; runtime limit signals from Codex sessions still count as real exhaustion even when the displayed percentage is not exactly 100%
- while a Desktop task or spawned child agent is still active, auto-rotation records a pending switch and waits until that work becomes idle
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
codex-auth-pool daemon --interval-seconds 600
```

## Upgrading

After pulling new code, rerun:

```bash
./install.sh
```

This now reinstalls the package and attempts to reload any existing background service automatically.

## License

MIT

# Codex Auth Pool

一个给 Codex Desktop 用的多账号自动轮换工具，在保留官方 ChatGPT 登录和 `computer use` 的前提下，让多个账号按额度自动切换。

[English README](./README.md)

## 这个项目解决什么问题

如果你是用官方 `codex login` 或 ChatGPT 登录态来用 Codex，通常会遇到两个痛点：

- 单个账号额度不够
- 手动切账号很麻烦，而且容易切错

`codex-auth-pool` 的目标，就是把这些账号做成一个本地账号池：

- 保存多个官方登录态
- 兼容导入 `cliproxyapi` 的 auth
- 统一管理为可轮换的账号池
- 自动查询真实 5 小时额度和周额度重置时间
- 自动切到下一个可用账号
- 不破坏 Codex Desktop 的 `computer use`

## 适合谁用

- 使用 Codex Desktop 的 macOS 用户
- 使用 Codex CLI 的 Ubuntu/Linux 用户，需要账号轮换但不依赖 Desktop 专属能力
- 有多个 ChatGPT / Codex 账号的人
- 不想再手动复制 `auth.json` 来回切换的人
- 希望切号时尽量保留本地插件、连接器和环境状态的人

## 工作原理

它管理的是你 home 目录下的全局状态，而不是某个项目目录：

- `~/.codex/`：Codex 自己的登录、插件、session
- `~/.codex-auth-pool/`：本工具自己的配置、账号池、日志、快照
- `~/.cli-proxy-api/`：可选的 `cliproxyapi` 导入来源

账号池里的 managed profile 保存为原生 Codex `auth.json` 格式，所以即便你不用命令，也能手动复制到：

- `~/.codex/cache/auth.json`
- `~/.codex/auth.json`

来做紧急切换。

## 主要功能

- 保存多个官方 `codex login` 登录态，避免后一次登录覆盖前一次。
- 后台轮换或 `apply-best` 覆盖 auth 前，会先检查当前官方登录是否已在池子中；如果不在，会按 `account_id` 自动保存到 managed vault。
- 导入现有的 `cliproxyapi` 账号。
- 在查看状态、打开看板、刷新额度、选择账号和后台轮换时自动发现新加入的 `cliproxyapi` Codex 账号。
- 自动把新 `cliproxyapi` 账号吸收到 managed vault，并补齐第一次真实额度观测。
- 直接查询 `https://chatgpt.com/backend-api/wham/usage`，获取每个账号真实的额度窗口和重置时间。
- 排序时优先使用真实观测值，而不是只依赖本地元数据。
- 把 `~/.codex/auth.json` 和 `~/.codex/cache/auth.json` 视为同一个有效登录态；如果两者漂移，后台守护会先自动对齐，再判断额度。
- 账号额度触顶后自动冷却，并切换到下一个可用账号。
- App/Desktop 自动轮换使用 app 策略：可用 Pro 账号排在 Plus 前面；所有 Pro 都不可用或额度耗尽后，才回退到 Plus。
- CLI goal 自动恢复使用独立 CLI 策略：Plus 优先，其次 Free，最后 Pro，避免 Plus 全部耗尽时长任务停滞。
- 提供 `codex-plus` 命令：普通 CLI 手动任务也可以走独立 `CODEX_HOME`，不会覆盖 Codex Desktop 当前使用的 Pro/Plus auth。
- `codex-plus` 会复用 `~/.codex` 的 sessions、plugins、skills、config 等状态，所以 `codex resume` 和已安装插件不需要另建一套。
- 当前账号 auth token 过期时（`HTTP 401 token_expired`），会视为账号不可用并自动切走，不再继续相信旧额度快照。
- 如果 Codex 会话日志已经出现运行时限额信号（`usage_limit_exceeded`、`rate_limit_reached_type`，或连续 `rate_limits=null`），即使界面百分比没有精确显示 100%，也会按真实耗尽处理。
- macOS 上切换后可自动重启 Codex Desktop。
- 自动重启前会记录最近活跃的 Codex Desktop 会话，重启后通过 Codex app-server 协议对这些原 `threadId` 发送 `继续`。
- 恢复会话使用 `thread/resume` + `turn/start`，不再另起一个 `codex exec resume` 后台代理会话。
- CLI auth 自动轮换后会检测 active goal 线程，并在 macOS Terminal 中执行 `codex resume <thread_id>`，让 CLI 长任务继续跑在选中的 CLI auth 上。
- 后台守护只有在真实额度触发阈值时才会切换和重启；普通轮询不会打断当前工作。
- 内置防重入锁和短时间自动轮换节流，避免重复 tick 导致连续切号/重启。
- 支持快照和恢复本地插件、配置、连接器缓存状态。
- 环境快照会保存 Browser Use 的本地 Electron 浏览器状态，包括 `Cookies`、`Local Storage`、`Session Storage` 和 `Partitions/codex-browser-app`。
- 自动切号重启时只恢复 Browser Use 需要的浏览器登录态，不再用旧快照覆盖 `~/.codex/plugins`，避免切号后插件被回滚、需要重新安装。
- 可把当前可用账号导出到 `~/.codex/ready-auths/`，里面每个 `*.auth.json` 都是可以手动复制到 Codex `auth.json` 的原生格式。
- 支持 macOS `launchd` 后台常驻。
- 支持 Ubuntu/Linux `systemd --user` 后台常驻。

## 安装

### 最简单

```bash
git clone https://github.com/zzt5678/codex-auth-pool.git
cd codex-auth-pool
./install.sh
```

如果你已经装过后台轮换器，现在重新执行 `./install.sh` 也会自动重载已存在的 `launchd` 或 `systemd --user` 服务，让它立即切到新代码。

### 手动安装

```bash
git clone https://github.com/zzt5678/codex-auth-pool.git
cd codex-auth-pool
pipx install .
```

## 快速开始

### 1. 先检查环境

```bash
codex-auth-pool check
codex-auth-pool doctor
```

### 2. 执行首次初始化

macOS：

```bash
codex-auth-pool init --install-launchd
```

Ubuntu/Linux：

```bash
codex-auth-pool init --install-systemd
```

这一步会自动完成：

- 写入配置文件
- 备份一份本地 Codex 环境快照
- 保存当前官方登录
- 迁移旧格式 managed profile
- 导入 `cliproxyapi` 账号
- 按需安装后台自动轮换

### 2.1 如果你要让 CLI 只用 Plus

安装后会同时提供 `codex-plus`：

```bash
codex-plus
codex-plus resume <thread_id>
codex-plus --version
```

它会在运行前按 `Plus -> Free -> Pro` 自动选择当前可用的 CLI 账号，写入隔离目录 `~/.codex-auth-pool/cli-plus-home`，并通过 `CODEX_HOME` 启动官方 `codex`。这不会改写 Codex Desktop 使用的 `~/.codex/auth.json` 或 `~/.codex/cache/auth.json`。

如果只想准备隔离 home、不启动 CLI：

```bash
codex-auth-pool cli-prepare
```

### 3. 打开看板

```bash
codex-auth-pool dashboard
```

这是普通用户最值得看的命令。它会展示：

- 当前账号
- 当前 5 小时额度和周额度使用情况
- 下一个准备切换的账号
- 这个重置时间是不是 `observed`
- 是否自动导入并观测到了新的 `cliproxyapi` 账号
- 当前被打断会话恢复时会按什么模型顺序尝试
- 后台守护是否正常运行

新加入的 `cliproxyapi` Codex 账号不需要手动执行 `sync-cliproxy`。
运行 `dashboard`、`status`、`pick`、`apply-best`、`refresh-usage`，或者后台守护执行 `tick` 时，工具都会自动吸收新账号并补齐初始额度观测。

如果你通过官方 `codex login` 临时登录了一个新账号，后台轮换或 `apply-best` 在覆盖当前 auth 前会自动把这个当前登录态保存进池子，避免有效账号被下一个切号覆盖。这个保护按 `account_id` 去重，不会重复保存同一个账号。

### 4. 主动刷新真实额度

```bash
codex-auth-pool refresh-usage --force
```

这条命令会逐个账号向 ChatGPT 查询真实额度窗口，并更新缓存的重置时间。

### 5. 无破坏性检查是否会触发轮换

```bash
codex-auth-pool tick --dry-run
codex-auth-pool forecast
codex-auth-pool report --no-discover
codex-auth-pool fix
codex-auth-pool events --limit 10
```

`tick --dry-run` 只报告是否会触发轮换，不会写入冷却、不切号、不重启。
`forecast` 会在一个屏幕里说明当前账号、下一个账号、额度来源、后台状态，以及接下来会不会切号。
`report` 输出同样信息的 JSON，方便后续做面板、脚本或排障。
`fix` 默认是 dry-run，只预览低风险修复；确认后用 `fix --apply` 同步 auth 文件、清理过期冷却或补齐缺失元数据。
`events` 默认输出易读摘要；如果需要原始 JSONL，可以使用 `codex-auth-pool events --raw`。

### 6. 运行本地测试

```bash
python -m unittest discover -s tests
```

测试覆盖核心轮换规则、运行时限额信号、Browser Use 活跃保护、候选账号短期失败冷却、CLI goal 恢复判断，以及 `codex-plus` 隔离 home 不覆盖全局 auth。

## Token 使用量和成本估算

`token-usage` 会扫描本机 Codex rollout 日志，按账号、模型、线程，或账号/模型组合统计 token 消耗：

```bash
codex-auth-pool token-usage
codex-auth-pool token-usage --by model --since 2026-05-01
codex-auth-pool token-usage --by thread --limit 20
codex-auth-pool token-usage --json
```

输出会包含输入 token、缓存输入 token、非缓存输入 token、输出 token、reasoning 输出 token、按 OpenAI API 标准价格估算的美元成本，以及按 Codex token-based rate card 估算的 Codex credits。这个结果来自本地日志，只适合看趋势和大致消耗，不是 ChatGPT Plus 的官方账单。

## 被重启打断的会话恢复

通过 `launchd-install`、`systemd-install`、`setup --install-*` 或 `init --install-*` 安装后台服务时，默认会启用切号后重启。macOS 上自动轮换切号后，工具会做一个保守的恢复流程：

- 软触发时，如果 Desktop 会话仍在执行，会把轮换写入 `pending_rotation`，等会话空闲后自动切换，不会丢掉这次切换信号
- 硬耗尽时也会先等待活跃 Desktop 任务结束；默认没有强制切换倒计时，只有活跃会话空闲后才切换账号
- 如果检测到正在运行的子 agent / spawned thread，会继续等待子 agent 结束后再切换
- 重启 Codex Desktop 前，从 `~/.codex/state_5.sqlite` 和 `~/.codex/logs_2.sqlite` 捕获最近活跃的 Desktop 会话
- active goal 线程不会被当成 Desktop 会话阻塞切号；它只会在 goal 自己遇到额度/认证阻塞后，走独立的 `codex resume <thread_id>` 恢复流程
- active goal 恢复使用独立 CLI 策略：Plus 优先，其次 Free，最后 Pro；不会因为 Plus 全部不可用而停滞
- goal 恢复前会先看 rollout 是否仍在产生事件；最近仍有进展会延迟恢复，只有在最新进展之后出现明确额度/认证错误才执行 `codex resume`，单纯长时间无日志不会误开第二个长任务
- goal 自动恢复成功后，只会终止同一个 `thread_id` 的旧 `codex resume` 进程树，避免旧终端任务继续卡在限额错误；不会关闭其他普通终端或 Desktop 会话
- Codex Desktop 重新启动后，后台启动轻量恢复 helper，对每个捕获到的 `threadId` 执行 `thread/resume` 和 `turn/start`
- 恢复只走原 Desktop 线程路径；不再降级到 `codex exec resume`，因为那可能创建单独 CLI 恢复，而不是继续原 Desktop 会话
- 会话快照和恢复日志保存在 `~/.codex-auth-pool/session-recovery/`

后台守护进程独立于当前 Codex 账号运行，所以即便当前账号额度已经归零、Codex App 不能继续回答，守护进程仍然可以读取 pending 状态，并在活跃 Desktop 任务空闲后替换 auth、重启 Codex。默认没有强制切换倒计时：

```bash
codex-auth-pool launchd-install --hard-active-grace-seconds 0
```

如果你明确需要强制切换倒计时，把 `0` 换成对应秒数。

如果你只想自动重启，不想自动对会话发送 `继续`：

```bash
codex-auth-pool launchd-install --no-resume-interrupted-sessions
```

如果你不想在切号后自动恢复 active goal 对应的 CLI 会话：

```bash
codex-auth-pool launchd-install --no-resume-active-goals
```

如果你明确只想切 auth、不想重启 Codex Desktop：

```bash
codex-auth-pool launchd-install --no-restart-after-switch
```

## API 会话兼容 ChatGPT 登录

如果以前通过 `cliproxyapi` 或其他 API provider 产生过本地会话，ChatGPT 官方登录模式下可能无法直接打开这些历史线程。可以先预览：

```bash
codex-auth-pool sessions-compat
```

确认要迁移某个线程时：

```bash
codex-auth-pool sessions-compat --apply --thread-id <thread_id>
```

如果你明确想一次性迁移全部匹配的 API-provider 本地线程：

```bash
codex-auth-pool sessions-compat --apply --all
```

这个命令只改 `~/.codex/state_5.sqlite` 里的本地索引，把 `model_provider` 改为 `openai`；不会修改 rollout 内容。执行前会自动备份数据库。

## 最常用命令

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

`export-ready-auths` 会把当前未过期、未冷却、未被真实额度窗口阻塞的账号导出到：

```bash
~/.codex/ready-auths/
```

紧急手动切换时，把其中一个 `*.auth.json` 复制到 `~/.codex/cache/auth.json` 和 `~/.codex/auth.json`，然后完整重启 Codex Desktop。

## 排序和轮换规则

轮换会优先选择这些账号：

1. 没有被禁用
2. 没有过期
3. 不在冷却中
4. 没有被真实远程限额窗口阻塞
5. 符合当前使用策略
6. App/Desktop 自动轮换：Pro 优先，然后 Plus，最后才是 Free/未知套餐
7. CLI goal 自动恢复和 `codex-plus`：Plus 优先，其次 Free，最后 Pro
8. 真实观测到的周重置时间更早
9. 如果没有真实观测值，再看本地 `weekly_reset_at`
10. 最后再参考 auth 元数据的新鲜度

这个策略不会因为池子里出现 Pro 账号就主动重启 Codex。它只会在已经出现真实额度/认证触发、必须切号时改变候选账号排序，因此不会破坏之前“非必要不重启”的体验。手动 `apply-best` 默认使用 App/Desktop 策略；如果你明确想按 CLI 顺序选择候选，可以运行 `codex-auth-pool apply-best --account-policy cli`，其顺序是 `Plus -> Free -> Pro`。如果你要启动普通 CLI 长任务，优先用 `codex-plus`，它不会改变 App 当前 auth。

`refresh-usage` 会把真实查询结果写入账号对应的元数据 sidecar。
对于 managed vault 里的账号，sidecar 会保存在账号文件旁边的 `.meta.json`。
对于从 `cliproxyapi` 导入的源账号，元数据会写到 `~/.codex-auth-pool/source-meta/`，不会污染原始的 `~/.cli-proxy-api/` 目录。

当你在界面里看到：

- `reset_source: observed`

就表示这个时间是 ChatGPT 真实返回的，不是本地猜测值。

## 常用命令总览

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

## 路径说明

优先级顺序：

1. 命令行参数
2. 环境变量
3. `~/.codex-auth-pool/config.json`
4. 内置默认值

重要目录：

- 配置：`~/.codex-auth-pool/config.json`
- 托管账号：`~/.codex-auth-pool/profiles/`
- 状态：`~/.codex-auth-pool/state.json`
- 事件日志：`~/.codex-auth-pool/events.jsonl`
- 环境快照：`~/.codex-auth-pool/env-snapshots/`
- launchd 日志：
  - `~/.codex-auth-pool/logs/launchd.stdout.log`
  - `~/.codex-auth-pool/logs/launchd.stderr.log`
- systemd 日志：
  - `~/.codex-auth-pool/logs/systemd.stdout.log`
  - `~/.codex-auth-pool/logs/systemd.stderr.log`

## 备注

- macOS 支持切换后自动重启 Codex Desktop
- Ubuntu/Linux 支持账号轮换和 `systemd --user`，但自动重启 Codex Desktop 会自动降级为 no-op
- 会同时更新 `~/.codex/cache/auth.json` 和 `~/.codex/auth.json`
- `status` 和 `dashboard` 会显示当前生效的 auth 文件，以及 root/cache 是否同步
- 插件和连接器状态尽量与 auth 轮换解耦
- Browser Use 可用时先授权一次，然后执行 `codex-auth-pool snapshot-env --name browser-use-working-$(date +%Y%m%d-%H%M%S)`；之后自动切号重启会在重新打开 Codex 前恢复这个快照
- `apply-best --restart-after-switch` 是人工立即切换命令；后台自动切换请使用 `init --install-launchd` 或 `launchd-install`。后台服务默认会在切号后重启 Codex；只有明确需要“只切 auth 不重启”时才加 `--no-restart-after-switch`
- 后台轮换默认不做百分比提前切换：
  - 5 小时窗口默认阈值 `100%`
  - 周窗口默认阈值 `100%`
  - 如果 Codex 运行时已经返回限额信号，即使百分比没有显示 100%，也会按真实耗尽处理
  - 活跃 Desktop 任务或子 agent 仍在运行时，只写入 pending，等空闲后再切换
- 如果当前没有可切账号，`status`、`dashboard` 和后台事件会显示被阻塞账号的原因，以及最早可能恢复的时间

## Ubuntu 部署

前置条件：

- Python 3.10+
- `git`
- 如果需要后台常驻，需要可用的 `systemd --user`
- 已经存在 `~/.codex/` 官方登录态，或者有可导入的 `~/.cli-proxy-api/` auth 文件

推荐安装方式：

```bash
git clone https://github.com/zzt5678/codex-auth-pool.git
cd codex-auth-pool
./install.sh
codex-auth-pool init --install-systemd
codex-auth-pool dashboard
```

如果你的 Ubuntu 环境没有 `systemctl --user`，可以手动跑守护：

```bash
codex-auth-pool daemon --interval-seconds 600
```

## 升级

拉取新代码后，重新执行：

```bash
./install.sh
```

现在它会重新安装包，并尝试自动重载已经存在的后台服务，避免“命令已经是新代码，但守护还在跑旧代码”的情况。

## 许可证

MIT

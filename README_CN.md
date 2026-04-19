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
- 导入现有的 `cliproxyapi` 账号。
- 直接查询 `https://chatgpt.com/backend-api/wham/usage`，获取每个账号真实的额度窗口和重置时间。
- 排序时优先使用真实观测值，而不是只依赖本地元数据。
- 账号额度触顶后自动冷却，并切换到下一个可用账号。
- macOS 上切换后可自动重启 Codex Desktop。
- 支持快照和恢复本地插件、配置、连接器缓存状态。
- 支持 macOS `launchd` 后台常驻。
- 支持 Ubuntu/Linux `systemd --user` 后台常驻。

## 安装

### 最简单

```bash
git clone https://github.com/zzt5678/codex-auth-pool.git
cd codex-auth-pool
./install.sh
```

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
codex-auth-pool init --install-launchd --restart-after-switch
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

### 3. 打开看板

```bash
codex-auth-pool dashboard
```

这是普通用户最值得看的命令。它会展示：

- 当前账号
- 当前 5 小时额度和周额度使用情况
- 下一个准备切换的账号
- 这个重置时间是不是 `observed`
- 后台守护是否正常运行

### 4. 主动刷新真实额度

```bash
codex-auth-pool refresh-usage --force
```

这条命令会逐个账号向 ChatGPT 查询真实额度窗口，并更新缓存的重置时间。

## 最常用命令

```bash
codex-auth-pool dashboard
codex-auth-pool status
codex-auth-pool refresh-usage --force
codex-auth-pool save-current --name my-official-1
codex-auth-pool sync-cliproxy
codex-auth-pool apply-best --restart-after-switch
codex-auth-pool launchd-status
codex-auth-pool systemd-status
```

## 排序和轮换规则

轮换会优先选择这些账号：

1. 没有被禁用
2. 没有过期
3. 不在冷却中
4. 没有被真实远程限额窗口阻塞
5. 真实观测到的周重置时间更早
6. 如果没有真实观测值，再看本地 `weekly_reset_at`
7. 最后再参考 auth 元数据的新鲜度

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
codex-auth-pool pick
codex-auth-pool check
codex-auth-pool doctor
codex-auth-pool save-current --name my-official-1
codex-auth-pool import-auth-file ~/.codex/auth.json --name imported-official
codex-auth-pool sync-cliproxy
codex-auth-pool refresh-usage --force
codex-auth-pool apply-best --restart-after-switch
codex-auth-pool tick
codex-auth-pool launchd-install --interval-seconds 60 --restart-after-switch
codex-auth-pool launchd-status
codex-auth-pool systemd-install --interval-seconds 60
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
- 插件和连接器状态尽量与 auth 轮换解耦
- 后台轮换默认是提前切换：
  - 5 小时窗口默认阈值 `95%`
  - 周窗口默认阈值 `98%`

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
codex-auth-pool daemon --interval-seconds 60
```

## 许可证

MIT

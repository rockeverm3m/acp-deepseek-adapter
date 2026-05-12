# DeepSeek TUI ↔ cc-connect

Bridge DeepSeek TUI to cc-connect via ACP — chat with DeepSeek from **Feishu, WeChat, QQ, Discord, Telegram**.  
通过 cc-connect ACP 协议把 DeepSeek TUI 接入**飞书、微信、QQ、Discord、Telegram**，手机就是终端。

> Built by talking to DeepSeek TUI through Feishu, overnight, by someone who doesn't code.  
> 本项目由 DeepSeek TUI 自身辅助开发完成。

## Slash Commands / 斜杠命令

| Command | Description |
|---------|-------------|
| `/dir <path>` | Switch working directory. `/dir -` goes back / 切换工作目录 |
| `/mode default\|yolo` | Change permission mode / 权限模式切换 |
| `/model v4-pro\|v4-flash` | Switch DeepSeek model / 模型切换 |
| `/new [name]` | New session / 新建会话 |
| `/list` | List sessions / 列出会话 |
| `/current` | Current session info / 当前会话 |
| `/memory` | Read/write AGENTS.md / 读写记忆文件 |
| `/compact` | Context usage & compaction status / 上下文压缩 |

Also: `[ctx: ~X%]` context display, visible tool calls, Feishu Markdown sanitization, persistent history.  
另有：上下文用量显示、工具调用可视、飞书 Markdown 防炸字体、对话历史持久化。

## Quick Start / 快速开始

### 1. Configure cc-connect / 配 cc-connect

Add a project to `~/.cc-connect/config.toml`:  
在 `~/.cc-connect/config.toml` 里加一个 project：

```toml
[[projects]]
name = "deepseek"

[projects.agent]
type = "acp"

[projects.agent.options]
command = "python3"
args = ["acp_deepseek_adapter.py"]
work_dir = "."

[[projects.platforms]]
type = "feishu"    # also: weixin, qq, discord, telegram / 也支持微信、QQ

[projects.platforms.options]
app_id = "your-feishu-app-id"
app_secret = "your-feishu-app-secret"
```

### 2. Environment / 环境变量

| Variable | Default | Description |
|----------|---------|-------------|
| `DEEPSEEK_BIN` | `~/deepseek` | Path to deepseek binary / 二进制路径 |
| `DEEPSEEK_WORKDIR` | `$HOME` | Working directory / 工作目录 |
| `ADAPTER_LOG_FILE` | `/tmp/deepseek-ccconnect.log` | Log file path / 日志 |

### 3. Start / 启动

cc-connect manages the adapter process automatically. Restart cc-connect after config changes.  
cc-connect 自动管理适配器进程，改完配置重启即可。

## Background / 背景

DeepSeek TUI already supports `deepseek serve --acp` (issue #782), but the tool_call notification pipeline isn't exposed yet. This adapter bridges that gap using `deepseek exec --auto` until native support lands.  
DeepSeek TUI 官方已支持 `serve --acp`，但工具调用链路还没暴露。本项目在官方做完之前，用 `deepseek exec --auto` 自己搭了一套完整的 ACP 桥接。

All pitfalls documented in [issue #1092](https://github.com/Hmbown/DeepSeek-TUI/issues/1092).  
踩过的坑都记在 issue #1092。

## Links / 链接

- [adapter repo / 适配器仓库](https://github.com/rockeverm3m/acp-deepseek-adapter)
- [DeepSeek TUI](https://github.com/Hmbown/DeepSeek-TUI)
- [cc-connect](https://github.com/cc-connect/cc-connect)

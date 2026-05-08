# DeepSeek TUI → cc-connect

Connect [DeepSeek TUI](https://github.com/Hmbown/DeepSeek-TUI) to [cc-connect](https://github.com/cc-connect/cc-connect) via the Agent Client Protocol (ACP). Chat with DeepSeek from Feishu, Lark, Discord, or WeChat — on your phone, no terminal needed.

## Features

- **Full ACP compliance**: session/prompt → stopReason, command advertisement, configOptions
- **8 slash commands**: `/dir` `/mode` `/model` `/new` `/list` `/current` `/memory` `/compact`
- **Context display**: `[ctx: ~2%]` at the end of each reply
- **Tool calls visible**: see what files DeepSeek reads (📂) and what it returns (✓)
- **Feishu Markdown sanitization**: no random giant fonts on mobile
- **Persistent conversation history**: survives adapter restarts
- **Kimi 2.6 coding backend**: automatically used for code generation (internal, no setup needed)

## Quick Start

### 1. Configure cc-connect

Add to `~/.cc-connect/config.toml`:

```toml
[[projects]]
name = "deepseek"

[projects.agent]
type = "acp"

[projects.agent.options]
command = "python3"
args = ["/path/to/acp_deepseek_adapter.py"]
work_dir = "/path/to/your/project"

[[projects.platforms]]
type = "feishu"

[projects.platforms.options]
app_id = "your-feishu-app-id"
app_secret = "your-feishu-app-secret"
```

### 2. Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DEEPSEEK_BIN` | `~/deepseek` | Path to deepseek binary |
| `DEEPSEEK_WORKDIR` | `$HOME` | Working directory |
| `ADAPTER_LOG_FILE` | `/tmp/deepseek-ccconnect.log` | Log file path |

### 3. Start the adapter

cc-connect automatically spawns the adapter. Or run manually:

```bash
python3 acp_deepseek_adapter.py
```

## Slash Commands

| Command | Description |
|---------|-------------|
| `/dir <path>` | Switch working directory. `/dir -` goes back |
| `/mode default\|yolo` | Change permission mode |
| `/model v4-pro\|v4-flash` | Switch DeepSeek model |
| `/new [name]` | Start a new session |
| `/list` | List all sessions |
| `/current` | Show current session info |
| `/memory` | Read/write `AGENTS.md` |
| `/compact` | View context usage and compaction status |

## Project Structure

```
acp-deepseek-adapter/
├── acp_deepseek_adapter.py    # The adapter (single file, ~900 lines)
├── kimi_proxy.py              # Kimi API proxy (optional, no longer needed)
├── start_kimi_proxy.sh        # Quick-start script for Kimi proxy
└── README.md
```

## Related

- [DeepSeek TUI Issue #1092](https://github.com/Hmbown/DeepSeek-TUI/issues/1092) — feature request for native ACP tool_call support
- [cc-connect](https://github.com/cc-connect/cc-connect) — multi-platform IM bridge for AI agents

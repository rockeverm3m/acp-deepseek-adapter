# ACP → DeepSeek TUI Adapter

通过 cc-connect 的 ACP 协议，在手机（飞书/微信/Telegram 等）上使用 deepseek-tui 的全部功能。

## 架构

```
手机(飞书) → cc-connect → ACP JSON-RPC 2.0 (stdio) → acp_deepseek_adapter.py → deepseek exec → DeepSeek API
```

## 文件

- `acp_deepseek_adapter.py` — 适配器主体 v2.0（基于真实 CLI: `deepseek-tui exec --json`）
- `cc-connect-config.toml` — cc-connect 项目配置模板

## 快速开始

### 1. 环境要求

- Python 3.10+
- deepseek-tui（`/Users/rk/deepseek`）
- cc-connect（`/Users/rk/.cc-connect/`）

### 2. 安装

```bash
# 确保适配器可执行
chmod +x /Users/rk/Developer/acp-deepseek-adapter/acp_deepseek_adapter.py

# 测试适配器是否能启动（Ctrl+C 退出）
python3 /Users/rk/Developer/acp-deepseek-adapter/acp_deepseek_adapter.py
# 看到 "ACP adapter started, listening on stdin..." 即可
```

### 3. 配置 cc-connect

将 `cc-connect-config.toml` 中的 `[[projects]]` 段合并到你现有的 `~/.cc-connect/config.toml`。

如果你希望 deepseek-tui 和 Claude Code 共存（不同 project），保留两个 `[[projects]]`，通过飞书 bot 的不同 app_id 区分。

### 4. 启动

```bash
cc-connect
```

### 5. 对话

在飞书里找到对应的 bot，直接发消息。支持：

- **普通对话** — 发送任意文本
- **/model** — 查看/切换模型
- **/mode** — 切换权限模式（Default / Plan / YOLO）
- **/new** — 新建会话
- **/list** — 列出会话
- **/switch** — 切换会话
- **/dir** — 查看/切换工作目录

## 功能覆盖

### 已实现的 ACP 方法

| ACP 方法 | 实现 | 说明 |
|----------|------|------|
| `initialize` | ✅ | 握手，返回能力和模式列表 |
| `authenticate` | ✅ | 认证（使用 config.toml） |
| `session/new` | ✅ | 创建新会话 |
| `session/load` | ✅ | 恢复已有会话 |
| `session/prompt` | ✅ | 发送消息，流式返回 |
| `session/set_mode` | ✅ | 切换权限模式（default/plan/yolo） |
| `session/list` | ✅ | 列出所有会话 |
| `session/update` | ✅ | 流式输出文本和工具调用状态 |

### 映射的 deepseek-tui 功能

| 终端功能 | 适配器映射 | 说明 |
|---------|-----------|------|
| 对话 | `deepseek exec --prompt "..."` | 非交互式执行 |
| 会话管理 | `deepseek sessions` + `--resume` | 跨消息保持上下文 |
| 计划模式 | `/mode plan` → `--plan-mode` | 先出计划再执行 |
| YOLO 模式 | `/mode yolo` → `--yolo` | 自动批准操作 |
| 读文件 | ✅ 通过 prompt 触发 | 说"读这个文件"即可 |
| 写文件 | ✅ 通过 prompt 触发 | default 模式下会请求审批 |
| 执行命令 | ✅ 通过 prompt 触发 | 说"运行这个命令"即可 |
| 代码审查 | ✅ 通过 prompt 触发 | 说"review 这个 diff"即可 |
| 子代理 | ✅ 通过 prompt 触发 | agent_spawn 自动并行 |
| 网页搜索 | ✅ 通过 prompt 触发 | web_search 工具 |
| 图片/文件 | ⚠️ 取决于平台 | 飞书支持图片转发 |

### 与终端版的差异

| 特性 | 终端版 | 适配器版 |
|------|--------|---------|
| 实时 plan/todo 侧边栏 | ✅ TUI 渲染 | ❌ 纯文本显示 |
| 交互式审批弹窗 | ✅ 终端内确认 | ⚠️ cc-connect `/mode` 控制 |
| 多行编辑 | ✅ 终端编辑 | ❌ 单条消息 |
| 上下文压缩 (/compact) | ✅ | ❌ (cc-connect 有自己的压缩) |
| 会话持久化 | ✅ | ✅ 通过 deepseek sessions |

## 调试

```bash
# 查看适配器日志
tail -f /tmp/acp-deepseek-adapter.log

# 查看 cc-connect 日志
tail -f /tmp/openclaw/openclaw-*.log
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEEPSEEK_BIN` | `/Users/rk/deepseek` | deepseek 二进制路径 |
| `DEEPSEEK_CONFIG` | `/Users/rk/.deepseek/config.toml` | 配置文件路径 |
| `DEEPSEEK_WORKDIR` | `/Users/rk` | 默认工作目录 |
| `DEEPSEEK_MODEL` | (空=使用配置) | 模型覆盖 |
| `ADAPTER_LOG_FILE` | `/tmp/acp-deepseek-adapter.log` | 日志路径 |

## 已知限制

1. **`deepseek exec` CLI 参数未完全验证** — `--resume`、`--plan-mode`、`--json` 等参数是基于 `--help` 输出的推测，实际可能需要调整 `_build_command()` 中的参数名
2. **流式输出格式** — `_emit_structured_update()` 假设 deepseek exec 输出 JSON Lines（每行一个 JSON），实际格式可能不同，需根据实际输出调整解析逻辑
3. **会话恢复** — 依赖 `deepseek sessions --json` 返回标准格式的会话列表，如果实际 API 不同，需调整 `_discover_thread_id()`
4. **上下文长度** — 通过 `exec` 模式每次都是完整的 prompt，没有增量上下文传递。如果需要真正的多轮对话，需要 deepseek-tui 支持 `--resume` 或 `--continue`

## 下一步优化

1. 用实际 `deepseek exec --help` 输出修正 CLI 参数
2. 根据实际 `deepseek exec` 输出格式调整流式解析
3. 如果 `exec` 不支持会话恢复，考虑用 `app-server` HTTP API 替代
4. 增加图片/文件转发支持（飞书多模态）

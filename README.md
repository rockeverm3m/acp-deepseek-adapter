# DeepSeek TUI → 飞书 / 微信 / QQ

通过 cc-connect 把 DeepSeek TUI 接入**飞书、微信、QQ、Discord、Telegram**。手机就是终端。

> 本项目由 DeepSeek TUI 自身辅助开发完成——一个不懂代码的小白，在飞书上对话一个通宵就做出来了。

## 能做什么

- ✅ 手机写代码、切目录、切模型
- ✅ 8 个斜杠命令：`/dir` `/mode` `/model` `/new` `/list` `/current` `/memory` `/compact`
- ✅ 每条回复末尾显示 `[ctx: ~2%]` 上下文用量
- ✅ 工具调用可视化（📂 看读了什么文件）
- ✅ 飞书 Markdown 防炸字体（`#` 不会变大标题）
- ✅ 对话历史持久化，重启不失忆
- ✅ Kimi 2.6 编码后端（内部自动调用，无感）

## 快速开始

### 1. 配 cc-connect

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
type = "feishu"    # 飞书。也可以换成 weixin、qq、discord、telegram

[projects.platforms.options]
app_id = "你的飞书 App ID"
app_secret = "你的飞书 App Secret"
```

### 2. 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEEPSEEK_BIN` | `~/deepseek` | deepseek 二进制路径 |
| `DEEPSEEK_WORKDIR` | `$HOME` | 工作目录 |
| `ADAPTER_LOG_FILE` | `/tmp/deepseek-ccconnect.log` | 日志 |

### 3. 启动

cc-connect 自动管理适配器进程，不需要手动启动。装好配置后重启 cc-connect 就行。

## 斜杠命令

| 命令 | 说明 |
|------|------|
| `/dir <路径>` | 切换工作目录，`/dir -` 返回上一个 |
| `/mode default\|yolo` | 权限模式切换 |
| `/model v4-pro\|v4-flash` | DeepSeek 模型切换 |
| `/new [名称]` | 新建会话 |
| `/list` | 列出所有会话 |
| `/current` | 当前会话信息 |
| `/memory` | 读写 `AGENTS.md` |
| `/compact` | 查看上下文和压缩状态 |

## 聊聊背景

DeepSeek TUI 官方已经支持 `deepseek serve --acp`（#782），但工具调用链路还没暴露给 ACP。本项目在官方做完之前，用 `deepseek exec --auto` 自己搭了一套完整的 ACP 桥接。

踩过的坑都记在 [issue #1092](https://github.com/Hmbown/DeepSeek-TUI/issues/1092)。

## 相关链接

- [适配器仓库](https://github.com/rockeverm3m/acp-deepseek-adapter)
- [DeepSeek TUI](https://github.com/Hmbown/DeepSeek-TUI)
- [cc-connect](https://github.com/cc-connect/cc-connect)

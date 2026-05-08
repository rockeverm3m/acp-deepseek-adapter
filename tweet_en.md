Spent an entire night and burned through roughly **270 million tokens** (~$75) building a complete ACP adapter for DeepSeek-TUI on top of `deepseek exec --auto`.

**What it does:**
- Full ACP protocol compliance: session/prompt → stopReason, command advertisement, configOptions
- 8 slash commands: `/dir` `/mode` `/model` `/new` `/list` `/current` `/memory` `/compact`
- Context usage indicator: `[ctx: ~X%]`
- Tool calls rendered as compact inline text instead of giant chat cards
- Feishu Markdown sanitization so headings don't blow up font sizes on mobile
- Auto-compaction threshold awareness (reads config.toml)

**Why this matters:**
For users in mainland China, DeepSeek is one of the few AI coding tools that works without a VPN. But the terminal TUI is intimidating for most people. By bridging it to Feishu (Lark) through cc-connect's ACP protocol, you can now **write code, switch directories, and change models — all from your phone.** For mobile-first developers, this is a game changer.

More importantly — it means **someone in China who doesn't know how to code can still ship an open-source product.** This adapter was built entirely by talking to DeepSeek through Feishu on a phone. Every commit, every bug fix, every feature — no terminal required.

Repo: github.com/rockeverm3m/acp-deepseek-adapter  
Issue with all the pitfalls documented: #1092

Hoping the official `serve --acp` will eventually support the full tool_call notification pipeline so this adapter layer isn't needed. If you think this direction is valuable, a share would mean a lot 🙏

*(P.S. This message was drafted by the adapter itself, running on DeepSeek-TUI through Feishu.)*

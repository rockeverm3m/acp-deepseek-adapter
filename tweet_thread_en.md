1/7
Spent a whole night & burned ~270M tokens (~$75) building a complete ACP adapter for @DeepSeek-TUI on top of `deepseek exec --auto`.
It lets you code from your phone. No terminal needed.

2/7
What it does:
• Full ACP protocol: session/prompt→stopReason, command ads, configOptions
• 8 slash commands: /dir /mode /model /new /list /current /memory /compact
• [ctx: ~X%] context indicator
• Tool calls as inline text (no giant chat cards)

3/7
• Feishu Markdown sanitization (no random giant fonts on mobile)
• Auto-compaction awareness (reads config.toml)
• stderr→stdout merge for tool call capture
• 9 commits, 8 tagged releases (v3.0→v3.6.1)

4/7
Why this matters:
In mainland China, DeepSeek is one of the few AI coding tools that works without a VPN. But the terminal TUI scares off most people. Bridge it to Feishu via cc-connect ACP, and suddenly you can write code from your phone.

5/7
More importantly: someone in China who doesn't know how to code can still ship an open-source product. This entire adapter was built by talking to DeepSeek through Feishu on a phone. Every commit, every bug fix — no terminal required.

6/7
Repo: github.com/rockeverm3m/acp-deepseek-adapter
All pitfalls documented in issue #1092.
Hoping official `serve --acp` eventually supports the full tool_call pipeline so this adapter layer isn't needed.

7/7
(P.S. This thread was drafted by the adapter itself, running on DeepSeek-TUI through Feishu. Yes, the tool wrote its own announcement.)

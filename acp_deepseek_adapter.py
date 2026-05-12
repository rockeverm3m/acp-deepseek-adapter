#!/usr/bin/env python3
"""
DeepSeek TUI ↔ cc-connect  v3.9.1
=================================
Bridge DeepSeek TUI to cc-connect (Feishu/WeChat/QQ/Discord/Telegram) via ACP.
通过 cc-connect ACP 协议把 DeepSeek TUI 接入飞书、微信、QQ 等 IM 平台。

Repo:    https://github.com/rockeverm3m/acp-deepseek-adapter
Issue:   https://github.com/Hmbown/DeepSeek-TUI/issues/1092

Supports: deepseek exec (agent mode), tool call passthrough, 8 slash commands,
          Kimi 2.6 backend, persistent history, Feishu Markdown sanitization,
          context usage display [ctx: ~X%].

Usage / 用法:
    python3 acp_deepseek_adapter.py

Environment / 环境变量:
    DEEPSEEK_BIN             path to deepseek binary  (default: ~/deepseek)
    DEEPSEEK_WORKDIR         working directory        (default: $HOME)
    ADAPTER_LOG_FILE         log file path            (default: /tmp/deepseek-ccconnect.log)
    ADAPTER_STRIP_THINKING   strip thinking tokens    (default: 1, set 0 to disable)
    ADAPTER_HIDE_TOOLS       hide tool call output     (default: 1, set 0 for debug)
"""

import hashlib
import json
import os
import re
import sys
import time
import uuid
import logging
import subprocess
import signal
import threading
import urllib.request
import urllib.error
from typing import Optional, Dict, Any, List

# ── Logging ──────────────────────────────────────────────────────────
LOG_FILE = os.environ.get("ADAPTER_LOG_FILE", "/tmp/deepseek-ccconnect.log")
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("acp-adapter")

# ── Configuration ────────────────────────────────────────────────────
DEEPSEEK_BIN = os.environ.get("DEEPSEEK_BIN",
    os.path.expanduser("~/deepseek"))
DEEPSEEK_WORKDIR = os.environ.get("DEEPSEEK_WORKDIR",
    os.path.expanduser("~"))
HISTORY_DIR = os.environ.get("ADAPTER_HISTORY_DIR",
    os.path.expanduser("~/.acp-adapter"))
HISTORY_MAX = 10  # keep last N message pairs

# ── Thinking filter ──────────────────────────────────────────────
# Strip DeepSeek V4 thinking tokens from cc-connect output.
# Set ADAPTER_STRIP_THINKING=0 to disable.
STRIP_THINKING = os.environ.get("ADAPTER_STRIP_THINKING", "1") not in ("0", "false", "no", "off")

# ── Tool output filter ─────────────────────────────────────────
# Hide individual tool calls (📂 read_file, ✓ result) from cc-connect IM channels.
# These internal process lines flood the chat and cause timeout truncation.
# Set ADAPTER_HIDE_TOOLS=0 to show full tool output (for debugging).
HIDE_TOOLS = os.environ.get("ADAPTER_HIDE_TOOLS", "1") not in ("0", "false", "no", "off")


# ── JSON-RPC 2.0 Transport ──────────────────────────────────────────
# Sentinel for deferred JSON-RPC responses (ACP session/prompt)
DEFERRED = object()

class JSONRPCTransport:
    """Newline-delimited JSON-RPC 2.0 over stdin/stdout."""

    def __init__(self):
        self._lock = threading.Lock()
        self._handlers: Dict[str, callable] = {}
        self._notif_handlers: Dict[str, callable] = {}
        self._running = True
        self._current_req_id: Any = None  # set by _dispatch before handler call

    def register(self, method: str, handler):
        self._handlers[method] = handler

    def register_notification(self, method: str, handler):
        self._notif_handlers[method] = handler

    def send_notification(self, method: str, params: Any):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def respond(self, req_id, result: Any = None, error: dict = None):
        msg = {"jsonrpc": "2.0", "id": req_id}
        if error:
            msg["error"] = error
        else:
            msg["result"] = result
        self._write(msg)

    def _write(self, msg: dict):
        line = json.dumps(msg, ensure_ascii=False)
        with self._lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def run(self):
        log.info("ACP adapter started, listening on stdin...")
        for line in sys.stdin:
            if not self._running:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.warning(f"Non-JSON input: {line[:100]}")
                continue
            self._dispatch(msg)
        log.info("stdin closed, shutting down.")

    def _dispatch(self, msg: dict):
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params", {})

        if method is not None and req_id is None:
            handler = self._notif_handlers.get(method)
            if handler:
                try:
                    handler(params)
                except Exception as e:
                    log.error(f"Notif handler error ({method}): {e}")
            return

        if method is not None:
            handler = self._handlers.get(method)
            if handler is None:
                self.respond(req_id, error={"code": -32601, "message": f"Method not found: {method}"})
                return
            try:
                self._current_req_id = req_id
                result = handler(params)
                if result is not DEFERRED:
                    self.respond(req_id, result=result)
            except Exception as e:
                log.error(f"Handler error ({method}): {e}", exc_info=True)
                self.respond(req_id, error={"code": -32603, "message": str(e)})
            return

    def stop(self):
        self._running = False

    def wait_pending(self, handlers=None):
        """Wait for pending execution threads (ACP session/prompt)."""
        if handlers and hasattr(handlers, '_exec_threads'):
            for t in handlers._exec_threads:
                t.join(timeout=30)


# ── Session State ────────────────────────────────────────────────────
class Session:
    def __init__(self, session_id: str, work_dir: str):
        self.id = session_id
        self.work_dir = work_dir
        self.deepseek_thread_id: Optional[str] = None
        self.mode = "default"
        self.model = "deepseek-v4-pro"  # default model
        self.dir_history: List[str] = []  # for /dir - (previous dir)
        self.history: List[dict] = []  # [{"role":"user","content":"..."}, ...]
        self.created_at: float = time.time()
        self._load_history()

    def _history_file(self) -> str:
        # Store per-project: <work_dir>/.deepseek-adapter/history.json
        hist_dir = os.path.join(self.work_dir, ".deepseek-adapter")
        os.makedirs(hist_dir, exist_ok=True)
        return os.path.join(hist_dir, "history.json")

    def _load_history(self):
        try:
            with open(self._history_file(), 'r') as fh:
                self.history = json.load(fh)
            log.info(f"Loaded {len(self.history)} history entries")
        except (FileNotFoundError, json.JSONDecodeError):
            self.history = []

    def save_history(self):
        try:
            with open(self._history_file(), 'w') as fh:
                json.dump(self.history[-HISTORY_MAX*2:], fh, ensure_ascii=False)
        except Exception as e:
            log.warning(f"History save failed: {e}")


class SessionManager:
    def __init__(self):
        self._sessions: Dict[str, Session] = {}

    def create(self, work_dir: str = None) -> Session:
        sid = f"ds-{uuid.uuid4().hex[:12]}"
        wd = work_dir or DEEPSEEK_WORKDIR
        sess = Session(sid, wd)
        self._sessions[sid] = sess
        log.info(f"Session created: {sid} (cwd={wd})")
        return sess

    def get(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    def list_sessions(self) -> List[Session]:
        return list(self._sessions.values())


# ── DeepSeek TUI Backend ────────────────────────────────────────────
class DeepSeekBackend:
    """Calls deepseek exec (agent mode, plain-text output)."""

    MODES = [
        {"id": "default", "name": "Default", "description": "每次操作前询问 (推荐)"},
        {"id": "yolo",   "name": "YOLO",    "description": "自动批准所有操作 (谨慎使用)"},
    ]

    MODELS = [
        {"value": "deepseek-v4-pro",   "name": "V4 Pro",   "description": "最强模型"},
        {"value": "deepseek-v4-flash", "name": "V4 Flash", "description": "快速模型"},
    ]

    _INTERNAL_MODELS = {"kimi-for-coding"}  # not exposed, auto-selected by agent

    # Kimi API (Anthropic format, no proxy needed)
    KIMI_URL = "https://api.kimi.com/coding/v1/messages"
    KIMI_KEY = os.environ.get("KIMI_API_KEY", "")

    # ── Streaming batch buffer ──────────────────────────────────
    # Buffer lines per-session and flush as paragraphs to reduce message
    # fragmentation in IM channels.
    # Strategy: send first chunk immediately for responsiveness, then
    # buffer aggressively and flush on paragraph boundaries or thresholds.
    _BUF_FLUSH_LINES = 20
    _BUF_FLUSH_CHARS = 800

    def __init__(self, transport: JSONRPCTransport):
        self.transport = transport
        self._bin = DEEPSEEK_BIN
        self._tool_counter = 0
        self._response_chars = 0  # per-execute character counter
        self._response_text = ""  # accumulated response text
        self._tool_depth = 0             # tool call nesting tracker
        self._any_tool_used = False      # set True on first tool call
        self._buf: Dict[str, List[str]] = {}    # session_id → lines
        self._buf_chars: Dict[str, int] = {}    # session_id → char count
        self._buf_sent_first: Dict[str, bool] = {}  # session_id → first chunk sent
        self._buf_flush_count: Dict[str, int] = {}  # session_id → flush counter

    # ── Command building ─────────────────────────────────────────
    def _build_command(self, prompt: str, session: Session) -> List[str]:
        """Build: deepseek exec [--auto] [--resume <id>] "<prompt>"

        NO --json flag — that switches to one-shot mode without tools.
        """
        cmd = [self._bin]

        # Model selection via --model flag (verified with v0.8.16)
        if session.model and session.model != "deepseek-v4-pro":
            cmd.extend(["--model", session.model])

        cmd.append("exec")

        # exec mode has no TTY for interactive approval → always use --auto
        # The user controls safety via cc-connect /mode (default/yolo both map to --auto here)
        cmd.append("--auto")

        # deepseek exec does NOT support --resume (verified v0.8.16)
        # Session continuity is not possible in exec mode.
        # if session.deepseek_thread_id:
        #     cmd.extend(["--resume", session.deepseek_thread_id])

        cmd.append(prompt)
        return cmd

    # ── Output streaming ─────────────────────────────────────────
    # deepseek exec streams stdout (preamble/answer) and stderr (tool calls).
    # See _stream_output docstring for two-pipe strategy.

    # Precompiled tool call patterns (used in _process_line)
    _TOOL_START_RE = re.compile(r"^tool:\s+(\S+)(?:\s*\((.*)\))?\s*$")
    _TOOL_DONE_RE = re.compile(r"^tool\s+(\S+)\s+completed(?::\s*(.*))?\s*$")

    # ── _STDOUT_TOOL_FILTER_RE ──
    # Tool call / diff / shell output / test output markers.
    _STDOUT_TOOL_FILTER_RE = re.compile(
        r'^(?:'
        r'tool[:\s]'                     # tool: or tool 
        r'|---\s'                        # --- a/path
        r'|\+\+\+\s'                     # +++ b/path
        r'|diff\s--git\s'               # diff --git a/...
        r'|index\s[0-9a-f]+'            # index aa6161d..40b6c67
        r'|---\sstdout|stderr'          # --- stdout/stderr ---
        r'|===\s[A-Z_]+\s==='            # === SECTION_HEADER ===
        r'|\bMATCH: |\bOK: |\bFAIL: '    # MATCH: / OK: / FAIL: test output
        r'|@@\s-\d+'                     # @@ -1,6 +1,6 @@
        r'|^Author:\s'                   # git log: Author: name <email>
        r'|^Date:\s'                     # git log: Date: 2026-05-13...
        r'|^Subject:\s'                  # git log: Subject: commit msg
        r'|^commit\s[0-9a-f]{8,}'       # git log: commit abc123...
        r'|^Merge:\s'                    # git log: Merge: abc123 def456
        r'|^\d+:\s'                      # grep -n: 1347: match
        r'|^\d+\s+\S+\.py'               # grep_files / wc -l: 1347 file.py
        r')'
    )

    # ── _JSON_METADATA_RE (multi-line aware) ──
    # Catches JSON lines from checklist_write / update_plan / internal TUI.
    # Matches standalone brackets, JSON key:value, or known internal keys.
    _JSON_METADATA_RE = re.compile(
        r'(?:^\s*[{\[][\s,]*$)'          # standalone { or [
        r'|(?:^\s*[}\]]\s*,?\s*$)'       # standalone } or ]
        r'|(?:^\s*"[a-z_]+"\s*:)'        # "key": value
        r'|(?:'
        r'"(?:items|plan|step|todos|status|id|content|'
        r'sessionId|update|stopReason|exitCode|sessionUpdate|'
        r'availableCommands|configOptions)'  # internal keywords
        r'"'
        r')'
    )

    # ── _SOURCE_HEADER_RE ──
    # Source-code header / README banner / adapter description / table lines,
    # plus git status short output and shebang / source-code lines.
    _SOURCE_HEADER_RE = re.compile(
        r'(?:^""")'                              # docstring opener
        r'|(?:^={3,}\s*$)'                        # =========== separator
        r'|(?:^#{1,4}\s)'                         # Markdown headings (banner)
        r'|(?:^#!)'                               # shebang: #!/usr/bin/env
        r'|(?:^\s*[MADRCU?][MADRCU? ]?\s+\S)'    # git status short:  M file.py
        r'|(?:DeepSeek TUI\s*[↔↔]\s*cc-connect)'  # adapter header
        r'|(?:Bridge DeepSeek TUI to cc-connect)' # README description
        r'|(?:通过 cc-connect ACP 协议)'            # Chinese adapter description
        r'|(?:通过 \.\.\.)'                       # truncated Chinese description
        r'|(?:Repo:\s+https?://github\.com/)'     # Repo: URL line
        r'|(?:Issue:\s+https?://github\.com/)'    # Issue: URL line
        r'|(?:Supports:|Usage / 用法:|Environment / 环境变量:)'  # adapter doc
        r'|(?:^\[?(?:Setup|用法|配置|环境))'          # section headers
        r'|(?:^\s*\|.*\|.*\|)'                     # Markdown table rows
    )

    # ── Two-pipe output streamer ────────────────────────────────
    # stdout: preamble (discarded when tools are used) or Q&A answer
    # stderr: tool calls + results (forwarded immediately)

    def _is_internal_line(self, line: str) -> bool:
        """Check if a line looks like internal TUI output, not user-facing text.

        Matches: diff markers, tool prefixes, JSON metadata, source headers.
        """
        if self._STDOUT_TOOL_FILTER_RE.match(line):
            return True
        if self._JSON_METADATA_RE.match(line):
            return True
        if self._SOURCE_HEADER_RE.match(line):
            return True
        return False

    def _process_line(self, line: str, session_id: str):
        """Process a single output line. Detects tool calls, emits text."""
        m = self._TOOL_START_RE.match(line)
        if m:
            self._emit_tool_call_text(session_id, self._next_tool_id(),
                                      m.group(1), m.group(2))
            return

        m = self._TOOL_DONE_RE.match(line)
        if m:
            if self._tool_depth > 0:
                self._tool_depth -= 1
                log.debug(f"Tool depth decrement: {self._tool_depth}")
            tool_result = m.group(2)
            if tool_result is None:
                return
            self._emit_tool_done_text(session_id, self._next_tool_id(),
                                      m.group(1), tool_result)
            return

        # Suppress between tool start/done when HIDE_TOOLS is active.
        if HIDE_TOOLS and self._tool_depth > 0:
            log.debug(f"Tool output suppressed (depth={self._tool_depth}): "
                      f"{line[:80]}")
            return

        # Phase 2: suppress internal TUI output even outside tool blocks.
        # Catches diff markers, JSON metadata, and code blocks that leak
        # between tool calls when the TUI writes non-tool lines to stderr.
        if HIDE_TOOLS and self._is_internal_line(line):
            log.debug(f"Internal line suppressed: {line[:80]}")
            return

        # Phase 3: after tools were used, drop pure-ASCII lines (source
        # code, diffs, shell output) that slipped past Phase 2.
        if HIDE_TOOLS and self._any_tool_used and not DeepSeekBackend._has_cjk(line):
            return

        self._emit_text(session_id, line)

    def _stream_stderr(self, pipe, session_id, tool_used):
        """Read stderr, emit tool calls immediately, set tool_used flag.

        Also strips thinking tags from stderr lines as a safety net —
        V4 may emit thinking tokens mixed into tool call output.
        """
        try:
            for raw_line in pipe:
                line = raw_line.rstrip('\n').rstrip('\r')
                if not line:
                    continue
                if STRIP_THINKING:
                    line = DeepSeekBackend._strip_thinking_tags(line)
                    if not line.strip():
                        continue  # line was entirely thinking
                if not tool_used[0]:
                    tool_used[0] = True
                self._process_line(line, session_id)
        except Exception as e:
            log.error(f"Stderr stream error: {e}")

    def _stream_output(self, process: subprocess.Popen, session_id: str):
        """Two-pipe streaming: buffer stdout, forward stderr immediately.

        After both pipes close:
          - stderr had content -> tools were used -> discard stdout preamble
          - stderr was empty    -> Q&A            -> flush stdout as answer
        """
        stdout_lines = []
        tool_used = [False]  # mutable cross-thread flag

        def read_stdout():
            try:
                for raw_line in process.stdout:
                    line = raw_line.rstrip('\n').rstrip('\r')
                    stdout_lines.append(line)
            except Exception as e:
                log.error(f"Stdout stream error: {e}")

        stderr_thread = threading.Thread(
            target=self._stream_stderr,
            args=(process.stderr, session_id, tool_used),
            daemon=True,
        )
        stdout_thread = threading.Thread(target=read_stdout, daemon=True)

        stderr_thread.start()
        stdout_thread.start()

        stdout_thread.join()
        stderr_thread.join()

        # ── Strip thinking tokens from stdout ──────────────────
        # ── Phase 0: tool depth tracking on stdout ─────────────
        # When HIDE_TOOLS is active and tools were used, suppress stdout
        # lines between tool: / tool X completed markers — this catches
        # file contents, shell output, and other tool results that leak
        # into stdout alongside the thinking preamble.
        #
        # This runs before thinking stripping so that tool blocks are
        # always suppressed regardless of STRIP_THINKING setting.
        stdout_depth = 0
        stdout_filtered = []
        for line in stdout_lines:
            if not line:
                stdout_filtered.append(line)
                continue
            if HIDE_TOOLS and tool_used[0]:
                if self._TOOL_START_RE.match(line):
                    stdout_depth += 1
                    log.debug(f"Stdout tool start (depth={stdout_depth}): "
                              f"{line[:80]}")
                    continue
                if self._TOOL_DONE_RE.match(line):
                    if stdout_depth > 0:
                        stdout_depth -= 1
                    log.debug(f"Stdout tool done (depth={stdout_depth}): "
                              f"{line[:80]}")
                    continue
                if stdout_depth > 0:
                    log.debug(f"Stdout tool suppressed "
                              f"(depth={stdout_depth}): {line[:80]}")
                    continue
            stdout_filtered.append(line)

        # ── Phase 1: strip thinking tokens ─────────────────────
        if STRIP_THINKING:
            text = "\n".join(stdout_filtered)
            prefix_cleaned = DeepSeekBackend._strip_thinking_prefix(text)
            prefix_stripped = len(text) - len(prefix_cleaned)
            if prefix_stripped > 0:
                log.debug(f"Thinking prefix stripped: {prefix_stripped} chars")
            clean = DeepSeekBackend._strip_thinking_tags(prefix_cleaned)
            xml_stripped = len(prefix_cleaned) - len(clean)
            if xml_stripped > 0:
                log.debug(f"XML thinking stripped: {xml_stripped} chars")
            total_stripped = len(text) - len(clean)
            if total_stripped > 0:
                log.info(f"Thinking filtered: {total_stripped} chars total "
                         f"(prefix={prefix_stripped}, xml={xml_stripped})")
            for line in clean.split("\n"):
                if HIDE_TOOLS and self._is_internal_line(line):
                    log.debug(f"Stdout internal line suppressed: {line[:80]}")
                    continue
                if HIDE_TOOLS and tool_used[0] and not DeepSeekBackend._has_cjk(line):
                    continue  # drop pure-ASCII lines after tool use
                self._emit_text(session_id, line)
        else:
            # Light filter: strip English-only thinking preamble even when
            # ADAPTER_STRIP_THINKING=0. Uses CJK-ratio scan to keep user's
            # language response while removing internal English narration.
            text = "\n".join(stdout_filtered)
            prefix_cleaned = DeepSeekBackend._strip_thinking_prefix(text)
            stripped = len(text) - len(prefix_cleaned)
            if stripped > 0:
                log.info(f"Light prefix stripped: {stripped} chars")
            for line in prefix_cleaned.split("\n"):
                if HIDE_TOOLS and self._is_internal_line(line):
                    log.debug(f"Stdout internal line suppressed: {line[:80]}")
                    continue
                if HIDE_TOOLS and tool_used[0] and not DeepSeekBackend._has_cjk(line):
                    continue  # drop pure-ASCII lines after tool use
                self._emit_text(session_id, line)

    def _next_tool_id(self) -> str:
        self._tool_counter += 1
        return f"tool_{self._tool_counter}"

    # Box-drawing characters used by deepseek in table output
    _BOX_DRAWING = set('│├└─┬┼┤┌┐┘')

    @staticmethod
    def _has_cjk(line: str) -> bool:
        """Return True if line contains at least one CJK character."""
        for ch in line:
            if ('\u4e00' <= ch <= '\u9fff'
                    or '\u3400' <= ch <= '\u4dbf'
                    or '\u3000' <= ch <= '\u303f'
                    or '\uff00' <= ch <= '\uffef'):
                return True
        return False

    # Precompiled thinking-tag patterns (used in _strip_thinking_tags)
    _THINKING_TAG_RES = [
        re.compile(r'<thinking>.*?</thinking>', re.DOTALL),
        re.compile(r'<思考>.*?</思考>', re.DOTALL),
        re.compile(r'<reasoning>.*?</reasoning>', re.DOTALL),
    ]

    @staticmethod
    def _strip_thinking_tags(text: str) -> str:
        """Remove DeepSeek V4 thinking tokens (XML-style blocks) from text.

        V4 emits thinking as ContentBlock::Thinking before the final answer.
        In exec mode these appear as <thinking>/<思考>/<reasoning> XML blocks.
        This strips them regardless of whether tools are used.
        """
        for pat in DeepSeekBackend._THINKING_TAG_RES:
            text = pat.sub('', text)
        return text

    @staticmethod
    def _strip_thinking_prefix(text: str) -> str:
        """Strip DeepSeek V4 thinking preamble from plain-text stdout.

        V4 emits thinking as plain-text process narration on stdout —
        internal monologue about files, tools, and analysis before the
        actual user-facing response. In cc-connect IM channels this
        floods the chat and causes timeout truncation.

        Strategy: scan backward by paragraph, find the first block with
        significant CJK content (>30% CJK characters). The thinking is
        almost always in English (tool names, code references, internal
        analysis), while the final response is in the user's language.
        """
        if not text:
            return text

        # ── Phase 1: CJK ratio boundary scan (paragraph-level) ──
        # Split into paragraph runs (1+ blank lines as separator).
        # Walk backward; the first CJK-heavy block marks the answer start.
        paragraphs = re.split(r'\n\n+', text)
        threshold = 0.30  # 30% CJK characters

        for i in range(len(paragraphs) - 1, -1, -1):
            para = paragraphs[i]
            if not para.strip():
                continue
            # Count CJK characters (U+4E00–U+9FFF, U+3400–U+4DBF,
            # plus CJK punctuation U+3000–U+303F, U+FF00–U+FFEF)
            cjk = sum(1 for ch in para
                      if ('\u4e00' <= ch <= '\u9fff'
                          or '\u3400' <= ch <= '\u4dbf'
                          or '\u3000' <= ch <= '\u303f'
                          or '\uff00' <= ch <= '\uffef'))
            total = len(para.strip())
            if total > 0 and cjk / total >= threshold:
                # Found the answer section — join from here onward
                return '\n\n'.join(paragraphs[i:])

        # ── Phase 2: fallback — structural boundary heuristics ──
        # If no CJK-heavy block found (e.g. pure-English answer),
        # try the old structural patterns.
        m = re.search(r'\n\n+#{1,4}\s', text)
        if m:
            return text[m.start() + 1:]

        m = re.search(r'\n\n+```', text)
        if m:
            return text[m.start() + 1:]

        m = re.search(r'\n\n+(?=[-*+]\s|\d+[.)]\s)', text)
        if m:
            return text[m.start() + 1:]

        # No boundary found — return as-is
        return text

    @staticmethod
    def _sanitize_feishu(text: str) -> str:
        """Escape leading Markdown that Feishu renders as giant headings."""
        # Escape # at line start (Feishu H1-H3) with zero-width space
        if text.lstrip().startswith('#'):
            # Find the first # and insert a zero-width space before it
            stripped = text.lstrip()
            leading = text[:len(text) - len(stripped)]
            text = leading + '\u200B' + stripped
        # Escape --- (horizontal rule) → rendered as a thin line in Feishu
        if text.strip() == '---':
            text = '\u200B---'
        # Escape box-drawing table lines — prevent Feishu table rendering
        if any(ch in DeepSeekBackend._BOX_DRAWING for ch in text):
            text = '\u200B' + text
        return text

    # ── Section separator between flushed chunks ──────────────────
    # Inserted between buffer flushes to create visible section breaks
    # in cc-connect's single-message-per-turn output.
    _SECTION_SEP = "\n\n---\n\n"

    def _flush_buffer(self, session_id: str):
        """Flush accumulated lines for session_id as a single text chunk."""
        lines = self._buf.pop(session_id, [])
        self._buf_chars.pop(session_id, None)
        if not lines:
            return
        text = "\n".join(lines)
        # Insert section separator between segments (not before first)
        count = self._buf_flush_count.get(session_id, 0)
        if count > 0:
            text = self._SECTION_SEP + text
        self._buf_flush_count[session_id] = count + 1
        self.transport.send_notification("session/update", {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text + "\n"},
            },
        })
        log.debug(f"Buffer flushed: {len(lines)} lines, {len(text)} chars")

    def _emit_text(self, session_id: str, text: str, buffered: bool = True):
        # Safety net: double-check internal lines at emit level
        if buffered and HIDE_TOOLS and self._is_internal_line(text):
            log.debug(f"Emit-level internal suppressed: {text[:80]}")
            return
        self._response_chars += len(text) + 1  # +1 for the newline we append
        # Only accumulate real content for history (skip tool/text noise
        # and known leak patterns to prevent history contamination)
        _NOISE_PREFIXES = ('📂', '  ✓', '[ctx:', '[退出码:', '[超时]', '[错误]',
                           '#!/usr/', '#!/bin/', 'M acp_', 'Author:', 'Date:',
                           'Subject:', 'commit ', 'Merge:', '通过 ...')
        if not text.startswith(_NOISE_PREFIXES):
            self._response_text += text + "\n"
        text = self._sanitize_feishu(text)
        if not buffered:
            # Send immediately (slash commands, errors, ctx line, etc.)
            self.transport.send_notification("session/update", {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text + "\n"},
                },
            })
            return
        # First-chunk: send the first non-empty line immediately for instant
        # feedback. Subsequent content is buffered and flushed as paragraphs.
        if not self._buf_sent_first.get(session_id):
            if text.strip():
                self._buf_sent_first[session_id] = True
                self.transport.send_notification("session/update", {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": text + "\n"},
                    },
                })
            return
        # After first chunk: buffer aggressively, flush only on threshold
        buf = self._buf.setdefault(session_id, [])
        buf.append(text)
        n = self._buf_chars[session_id] = self._buf_chars.get(session_id, 0) + len(text)
        # Flush on: double blank line (real paragraph boundary) or thresholds
        is_double_blank = (text.strip() == "" and len(buf) >= 2
                           and buf[-2].strip() == "")
        if is_double_blank or len(buf) >= self._BUF_FLUSH_LINES or n >= self._BUF_FLUSH_CHARS:
            self._flush_buffer(session_id)

    def _emit_tool_call_text(self, session_id: str, tool_id: str,
                              name: str, params: str):
        self._tool_depth += 1
        self._any_tool_used = True
        log.debug(f"Tool depth increment: {self._tool_depth} ({name})")
        self._response_chars += len(params)
        if not HIDE_TOOLS:
            self._emit_text(session_id, f"📂 {name}: {params[:100]}")
        else:
            log.debug(f"Tool hidden: {name}({params[:100]}) "
                      f"[depth={self._tool_depth}]")

    def _emit_tool_done_text(self, session_id: str, tool_id: str,
                              name: str, result: str):
        self._response_chars += len(result)
        if not HIDE_TOOLS:
            preview = result[:200] + ("…" if len(result) > 200 else "")
            self._emit_text(session_id, f"  ✓ {preview}")
        else:
            log.debug(f"Tool done hidden: {name}")

    # ── Execution ────────────────────────────────────────────────
    def execute(self, prompt: str, session: Session) -> dict:
        self._response_chars = 0  # reset per-call counter
        self._response_text = ""  # reset accumulated text
        self._tool_depth = 0      # safety reset
        self._any_tool_used = False
        # Reset buffer state for this execution
        self._buf_sent_first.pop(session.id, None)
        self._buf.pop(session.id, None)
        self._buf_chars.pop(session.id, None)
        self._buf_flush_count.pop(session.id, None)

        # ── Kimi For Coding path (direct HTTP, no deepseek exec) ──
        if session.model == "kimi-for-coding":
            return self._execute_kimi(prompt, session)

        cmd = self._build_command(prompt, session)
        log.info(f"Exec: {' '.join(cmd[:3])}... + prompt ({len(prompt)} chars)")

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,   # tool calls on stderr (processed in parallel)
                text=True,
                cwd=session.work_dir,
                env={**os.environ, "HOME": os.path.expanduser("~")},
            )

            stream_thread = threading.Thread(
                target=self._stream_output,
                args=(process, session.id),
                daemon=True,
            )
            stream_thread.start()

            try:
                returncode = process.wait(timeout=600)
            except subprocess.TimeoutExpired:
                process.kill()
                self._emit_text(session.id, "\n[超时] 命令执行超过10分钟，已终止。\n", buffered=False)
                return {"status": "timeout", "exitCode": -1}

            stream_thread.join(timeout=10)

            # Flush any remaining buffered text
            self._flush_buffer(session.id)

            if returncode != 0:
                self._emit_text(session.id, f"\n[退出码: {returncode}]\n", buffered=False)

            # _discover_thread_id disabled: deepseek exec does not support
            # --resume (verified v0.8.16), so thread IDs are unused.
            # Keep the discovery code for potential future exec mode support.
            # self._discover_thread_id(session)

            est_tokens = (self._read_system_tokens()
                          + len(prompt) // 2
                          + max(0, self._response_chars // 2))
            pct = min(est_tokens * 100 // 1_000_000, 99)
            self._emit_text(session.id, f"[ctx: ~{pct}%]", buffered=False)

            return {"status": "completed", "exitCode": returncode}

        except FileNotFoundError:
            msg = f"找不到 deepseek-tui: {DEEPSEEK_BIN}"
            log.error(msg)
            self._emit_text(session.id, f"\n[错误] {msg}\n", buffered=False)
            return {"status": "error", "message": msg}
        except Exception as e:
            log.error(f"Execution error: {e}", exc_info=True)
            self._emit_text(session.id, f"\n[错误] {e}\n", buffered=False)
            return {"status": "error", "message": str(e)}

    # ── Kimi direct API call ─────────────────────────────────────
    def _execute_kimi(self, prompt: str, session: Session) -> dict:
        """Call Kimi For Coding API directly (Anthropic Messages format)."""
        if not self.KIMI_KEY:
            msg = "KIMI_API_KEY 环境变量未设置，无法调用 Kimi API"
            log.error(msg)
            self._emit_text(session.id, f"\n[错误] {msg}\n", buffered=False)
            return {"status": "error", "message": msg}
        payload = {
            "model": "kimi-for-coding",
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(self.KIMI_URL, data=data, headers={
            "x-api-key": self.KIMI_KEY,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        })
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode(errors="replace")[:200]
            except Exception:
                body = e.reason or "unknown"
            msg = f"Kimi API error {e.code}: {body}"
            log.error(msg)
            self._emit_text(session.id, f"\n[错误] {msg}\n", buffered=False)
            return {"status": "error", "message": msg}
        except Exception as e:
            log.error(f"Kimi call failed: {e}")
            self._emit_text(session.id, f"\n[错误] Kimi 调用失败: {e}\n", buffered=False)
            return {"status": "error", "message": str(e)}

        # Extract text from Anthropic response
        text = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")

        usage = result.get("usage", {})
        total_tokens = usage.get("total_tokens", 0)
        log.info(f"Kimi done: {total_tokens} tokens, {len(text)} chars")

        self._emit_text(session.id, text, buffered=False)

        # Emit context (silent — only logged)
        est_tokens = self._read_system_tokens() + total_tokens
        pct = min(est_tokens * 100 // 1_000_000, 99)
        log.info(f"ctx: ~{pct}% | Kimi {total_tokens} tok (session={session.id})")

        return {"status": "completed", "exitCode": 0}

    # ── Session discovery ────────────────────────────────────────
    def _discover_thread_id(self, session: Session):
        """Find latest thread ID via deepseek sessions + thread list + disk scan."""
        tid = self._try_sessions_list() or self._try_thread_list() or self._scan_session_files()
        if tid and tid != session.deepseek_thread_id:
            session.deepseek_thread_id = tid
            log.info(f"Discovered thread: {tid}")

    def _try_sessions_list(self) -> Optional[str]:
        try:
            r = subprocess.run([self._bin, "sessions"], capture_output=True,
                               text=True, timeout=10, cwd=DEEPSEEK_WORKDIR)
            if r.returncode == 0 and r.stdout.strip():
                data = self._parse_table_or_json(r.stdout)
                if data:
                    return data[-1].get("id") or data[-1].get("thread_id")
        except Exception as e:
            log.debug(f"sessions list failed: {e}")
        return None

    def _try_thread_list(self) -> Optional[str]:
        try:
            r = subprocess.run([self._bin, "thread", "list"], capture_output=True,
                               text=True, timeout=10, cwd=DEEPSEEK_WORKDIR)
            if r.returncode == 0 and r.stdout.strip():
                data = self._parse_table_or_json(r.stdout)
                if data:
                    return data[-1].get("id")
        except Exception as e:
            log.debug(f"thread list failed: {e}")
        return None

    @staticmethod
    def _parse_table_or_json(stdout: str) -> List[dict]:
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            sessions = []
            for line in stdout.strip().split("\n"):
                parts = line.split()
                if not parts:
                    continue
                # Find the first hex-like token on the line (length >= 8).
                # This handles "* 284d7e7e | ..." (bullet prefix) and
                # "7907734d | ..." (no prefix), while rejecting prose like
                # "Continue", "Saved", "Resume", separator lines, etc.
                for word in parts:
                    if re.match(r'^[0-9a-f-]{8,}$', word):
                        sessions.append({"id": word})
                        break
            return sessions

    # ── Config / compact ─────────────────────────────────────────
    _compact_threshold: Optional[float] = None  # cached from config
    _system_tokens: Optional[int] = None  # cached system prompt estimate

    @classmethod
    def _read_system_tokens(cls) -> int:
        """Estimate system prompt token count from checkpoint (cached)."""
        if cls._system_tokens is not None:
            return cls._system_tokens
        ckpt = os.path.expanduser("~/.deepseek/sessions/checkpoints/latest.json")
        try:
            with open(ckpt, 'r') as fh:
                data = json.load(fh)
            cls._system_tokens = len(data.get("system_prompt", "")) // 2
        except Exception:
            cls._system_tokens = 30000  # fallback ~30K tokens
        return cls._system_tokens

    @classmethod
    def _read_compact_threshold(cls) -> Optional[float]:
        """Read auto_compact_threshold from ~/.deepseek/config.toml (cached)."""
        if cls._compact_threshold is not None:
            return cls._compact_threshold
        config_path = os.path.expanduser("~/.deepseek/config.toml")
        try:
            with open(config_path, 'r') as fh:
                for line in fh:
                    # TOML: key = "value"  or  key = value
                    m = re.match(r'^(?:auto_compact_threshold|compact_auto_threshold)\s*=\s*"?([0-9.]+)"?', line)
                    if m:
                        cls._compact_threshold = float(m.group(1))
                        return cls._compact_threshold
        except Exception:
            pass
        cls._compact_threshold = 0.5  # default
        return cls._compact_threshold

    def _get_session_info(self) -> Optional[dict]:
        """Read latest session file metadata (for /compact)."""
        sessions_dir = os.path.expanduser("~/.deepseek/sessions")
        if not os.path.isdir(sessions_dir):
            return None
        try:
            files = [f for f in os.listdir(sessions_dir)
                     if f.endswith(".json") and not f.startswith(".")]
            if not files:
                return None
            files.sort(key=lambda f: os.path.getmtime(
                os.path.join(sessions_dir, f)), reverse=True)
            latest = os.path.join(sessions_dir, files[0])
            with open(latest, 'r') as fh:
                data = json.load(fh)
            return data.get("metadata", {})
        except Exception as e:
            log.debug(f"session info read failed: {e}")
            return None

    @staticmethod
    def _scan_session_files() -> Optional[str]:
        sessions_dir = os.path.expanduser("~/.deepseek/sessions")
        if not os.path.isdir(sessions_dir):
            return None
        try:
            files = [f for f in os.listdir(sessions_dir)
                     if f.endswith(".json") and not f.startswith(".")]
            if not files:
                return None
            files.sort(key=lambda f: os.path.getmtime(
                os.path.join(sessions_dir, f)), reverse=True)
            return files[0].replace(".json", "")
        except OSError:
            return None

    def list_sessions(self) -> List[dict]:
        try:
            r = subprocess.run([self._bin, "sessions"], capture_output=True,
                               text=True, timeout=10, cwd=DEEPSEEK_WORKDIR)
            if r.returncode == 0:
                return self._parse_table_or_json(r.stdout)
        except Exception as e:
            log.warning(f"list_sessions: {e}")
        return []


# ── ACP Handlers ─────────────────────────────────────────────────────
class ACPHandlers:
    def __init__(self, transport: JSONRPCTransport):
        self.transport = transport
        self.backend = DeepSeekBackend(transport)
        self.sessions = SessionManager()

    # ── helpers ───────────────────────────────────────────────────
    def _config_options(self, session=None) -> list:
        mode_id = session.mode if session else "default"
        model_id = session.model if session else "deepseek-v4-pro"
        return [
            {"id": "mode", "name": "权限模式", "category": "mode", "type": "select",
             "currentValue": mode_id, "options": self.backend.MODES},
            {"id": "model", "name": "模型", "category": "model", "type": "select",
             "currentValue": model_id, "options": self.backend.MODELS},
        ]

    def _advertise_commands(self, session_id: str):
        self.transport.send_notification("session/update", {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "available_commands_update",
                "availableCommands": [
                    {"name": "compact", "description": "查看上下文和压缩状态"},
                    {"name": "dir", "description": "切换工作目录", "input": {"hint": "路径"}},
                    {"name": "mode", "description": "切换权限模式 (default/yolo)"},
                    {"name": "model", "description": "切换模型"},
                    {"name": "new", "description": "新建会话", "input": {"hint": "名称"}},
                    {"name": "list", "description": "列出所有会话"},
                    {"name": "current", "description": "查看当前会话"},
                    {"name": "memory", "description": "读写记忆文件"},
                ]
            }
        })

    # ── initialize ────────────────────────────────────────────────
    def handle_initialize(self, params: dict) -> dict:
        return {
            "protocolVersion": 1,
            "agentCapabilities": {
                "loadSession": True,
                "sessionCapabilities": {"list": True},
            },
            "serverInfo": {
                "name": "deepseek-ccconnect",
                "version": "3.9.1",
            },
            "modes": {
                "availableModes": self.backend.MODES,
                "currentModeId": "default",
            },
            "configOptions": self._config_options(),
        }

    # ── authenticate ──────────────────────────────────────────────
    def handle_authenticate(self, params: dict) -> dict:
        return {"status": "ok"}

    # ── session/new ───────────────────────────────────────────────
    def handle_session_new(self, params: dict) -> dict:
        cwd = params.get("cwd", DEEPSEEK_WORKDIR)
        if cwd and not os.path.isabs(cwd):
            cwd = os.path.join(DEEPSEEK_WORKDIR, cwd)
        s = self.sessions.create(work_dir=cwd)
        self._advertise_commands(s.id)
        return {
            "sessionId": s.id,
            "cwd": s.work_dir,
            "modes": {
                "availableModes": self.backend.MODES,
                "currentModeId": s.mode,
            },
            "configOptions": self._config_options(s),
        }

    # ── session/load ──────────────────────────────────────────────
    def handle_session_load(self, params: dict) -> dict:
        sid = params.get("sessionId", "")
        s = self.sessions.get(sid)
        if not s:
            s = self.sessions.create(work_dir=params.get("cwd", DEEPSEEK_WORKDIR))
        self._advertise_commands(s.id)
        return {
            "sessionId": s.id,
            "cwd": s.work_dir,
            "modes": {
                "availableModes": self.backend.MODES,
                "currentModeId": s.mode,
            },
            "configOptions": self._config_options(s),
        }

    # ── session/prompt ────────────────────────────────────────────
    def handle_session_prompt(self, params: dict):
        """ACP session/prompt — deferred response with stopReason (spec §4)."""
        sid = params.get("sessionId", "")
        prompt_blocks = params.get("prompt", [])

        prompt_text = ""
        for block in prompt_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                prompt_text += block.get("text", "")
            elif isinstance(block, str):
                prompt_text += block

        if not prompt_text.strip():
            return {"stopReason": "end_turn"}

        s = self.sessions.get(sid)
        if not s:
            s = self.sessions.create()
            sid = s.id

        # Handle slash commands locally
        stripped = prompt_text.strip()
        parts = stripped.split(maxsplit=1)
        cmd = parts[0] if parts else ""
        cmd_arg = parts[1] if len(parts) > 1 else ""

        if cmd == '/compact':
            threshold_pct = int(self.backend._read_compact_threshold() * 100)
            info = self.backend._get_session_info()
            msg_count = info.get("message_count", "?") if info else "?"
            title = info.get("title", "?") if info else "?"
            self.backend._emit_text(sid,
                f"自动压缩：已启用，阈值 {threshold_pct}%\n"
                f"当前会话：{title} ({msg_count} 条消息)")
            return {"stopReason": "end_turn"}

        elif cmd == '/new':
            new_s = self.sessions.create(work_dir=s.work_dir if s else DEEPSEEK_WORKDIR)
            name = cmd_arg or new_s.id
            self._advertise_commands(new_s.id)
            self.backend._emit_text(sid,
                f"新会话已创建\nID: {new_s.id}\n目录: {new_s.work_dir}")
            return {"stopReason": "end_turn", "sessionId": new_s.id}

        elif cmd == '/list':
            lines = ["会话列表:"]
            for sess in self.sessions.list_sessions():
                marker = "*" if sess.id == sid else " "
                lines.append(f"  {marker} {sess.id} | {sess.work_dir} | {sess.mode}")
            if not self.sessions.list_sessions():
                lines.append("  (无会话)")
            self.backend._emit_text(sid, "\n".join(lines))
            return {"stopReason": "end_turn"}

        elif cmd == '/current':
            self.backend._emit_text(sid,
                f"会话: {s.id}\n"
                f"目录: {s.work_dir}\n"
                f"模式: {s.mode}\n"
                f"模型: {s.model}")
            return {"stopReason": "end_turn"}

        elif cmd in ('/dir', '/cd'):
            if not cmd_arg:
                self.backend._emit_text(sid, f"当前目录: {s.work_dir}")
                return {"stopReason": "end_turn"}

            new_dir = cmd_arg
            # Support /dir - (previous directory)
            if new_dir == '-' and s.dir_history:
                new_dir = s.dir_history.pop()
            elif new_dir == '-':
                self.backend._emit_text(sid, "没有上一个目录")
                return {"stopReason": "end_turn"}

            if not os.path.isabs(new_dir):
                new_dir = os.path.join(s.work_dir, new_dir)
            new_dir = os.path.normpath(new_dir)

            if not os.path.isdir(new_dir):
                self.backend._emit_text(sid, f"目录不存在: {new_dir}")
                return {"stopReason": "end_turn"}

            old_dir = s.work_dir
            s.dir_history.append(old_dir)
            s.work_dir = new_dir
            self.backend._emit_text(sid, f"已切换到: {new_dir}")
            log.info(f"/dir: {sid} {old_dir} → {new_dir}")
            return {"stopReason": "end_turn"}

        elif cmd == '/mode':
            valid = [m["id"] for m in self.backend.MODES]
            if not cmd_arg:
                self.backend._emit_text(sid, f"当前模式: {s.mode}\n可选: {', '.join(valid)}")
                return {"stopReason": "end_turn"}
            if cmd_arg not in valid:
                self.backend._emit_text(sid, f"未知模式: {cmd_arg}. 可选: {', '.join(valid)}")
                return {"stopReason": "end_turn"}
            s.mode = cmd_arg
            self.backend._emit_text(sid, f"模式已切换为: {cmd_arg}")
            return {"stopReason": "end_turn"}

        elif cmd == '/model':
            valid = [(m["value"], m["name"]) for m in self.backend.MODELS]
            if not cmd_arg:
                current_name = next((n for v,n in valid if v == s.model), s.model)
                names = [f"{v} ({n})" for v,n in valid]
                self.backend._emit_text(sid, f"当前模型: {current_name}\n可选: {', '.join(names)}")
                return {"stopReason": "end_turn"}
            if cmd_arg not in [v for v,_ in valid]:
                names = ', '.join(v for v,_ in valid)
                self.backend._emit_text(sid, f"未知模型: {cmd_arg}. 可选: {names}")
                return {"stopReason": "end_turn"}
            s.model = cmd_arg
            name = next((n for v,n in valid if v == cmd_arg), cmd_arg)
            self.backend._emit_text(sid, f"模型已切换为: {name}")
            return {"stopReason": "end_turn"}

        elif cmd == '/memory':
            mem_file = os.path.join(s.work_dir, "AGENTS.md")
            if not cmd_arg:
                if os.path.exists(mem_file):
                    with open(mem_file, 'r') as fh:
                        content = fh.read()
                    preview = content[:2000]
                    more = f"\n... (共 {len(content)} 字符)" if len(content) > 2000 else ""
                    self.backend._emit_text(sid, f"AGENTS.md:\n{preview}{more}")
                else:
                    self.backend._emit_text(sid, "AGENTS.md 不存在。用 /memory add <内容> 创建。")
                return {"stopReason": "end_turn"}

            subcmd, _, text = cmd_arg.partition(' ')
            if subcmd == 'add':
                with open(mem_file, 'a') as fh:
                    fh.write('\n' + text + '\n')
                self.backend._emit_text(sid, f"已追加到 AGENTS.md")
                return {"stopReason": "end_turn"}
            elif subcmd == 'set':
                with open(mem_file, 'w') as fh:
                    fh.write(text + '\n')
                self.backend._emit_text(sid, f"已覆写 AGENTS.md")
                return {"stopReason": "end_turn"}
            else:
                self.backend._emit_text(sid, "用法: /memory | /memory add <内容> | /memory set <内容>")
                return {"stopReason": "end_turn"}

        # Build prompt with conversation history for continuity
        full_prompt = prompt_text
        if s.history:
            hist_lines = ["[对话历史 — 请记住以下内容以便保持上下文连贯]"]
            for entry in s.history[-HISTORY_MAX*2:]:
                role_label = "用户" if entry["role"] == "user" else "DeepSeek"
                hist_lines.append(f"{role_label}: {entry['content']}")
            hist_lines.append("[以上为历史记录，下面是最新消息]")
            full_prompt = "\n".join(hist_lines) + "\n\n" + prompt_text

        log.info(f"session/prompt: sid={sid}, len={len(prompt_text)}, history={len(s.history)}")

        # Execute synchronously — execute() blocks until deepseek finishes,
        # streaming session/update notifications for text and tool calls along the way.
        try:
            result = self.backend.execute(full_prompt, s)
            # Save to history after successful completion
            if result.get("status") == "completed":
                resp_text = self.backend._response_text.strip()
                if resp_text:
                    s.history.append({"role": "user", "content": prompt_text})
                    # Keep only first 500 chars of response to avoid history bloat
                    s.history.append({"role": "assistant", "content": resp_text[:500]})
                    s.save_history()
            status = result.get("status", "error")
            if status == "completed":
                log.info(f"session/prompt done: sid={sid}, stopReason=end_turn")
                return {"stopReason": "end_turn"}
            elif status == "timeout":
                return {"stopReason": "max_turn_requests"}
            else:
                return {"stopReason": "end_turn",
                        "error": result.get("message", "execution failed")}
        except Exception as e:
            log.error(f"Prompt error: {e}", exc_info=True)
            return {"stopReason": "end_turn",
                    "error": str(e)}

    # ── session/set_mode ──────────────────────────────────────────
    def handle_session_set_mode(self, params: dict) -> dict:
        sid = params.get("sessionId", "")
        mode_id = params.get("modeId", "default")
        s = self.sessions.get(sid)
        if not s:
            return {"error": f"session not found: {sid}"}
        valid = [m["id"] for m in self.backend.MODES]
        if mode_id not in valid:
            return {"error": f"unknown mode: {mode_id}. Valid: {valid}"}
        old = s.mode
        s.mode = mode_id
        log.info(f"set_mode: {sid} {old} → {mode_id}")
        return {"sessionId": sid, "modeId": mode_id, "previousModeId": old}

    # ── session/set_config_option ─────────────────────────────────
    def handle_session_set_config_option(self, params: dict) -> dict:
        sid = params.get("sessionId", "")
        config_id = params.get("configId", "")
        value = params.get("value", "")
        s = self.sessions.get(sid)
        if not s:
            return {"error": f"session not found: {sid}"}

        if config_id == "mode":
            valid = [m["id"] for m in self.backend.MODES]
            if value not in valid:
                return {"error": f"unknown mode: {value}. Valid: {valid}"}
            s.mode = value
            log.info(f"config mode: {sid} → {value}")
        elif config_id == "model":
            valid = [m["value"] for m in self.backend.MODELS]
            if value not in valid:
                return {"error": f"unknown model: {value}. Valid: {valid}"}
            s.model = value
            log.info(f"config model: {sid} → {value}")
        else:
            return {"error": f"unknown config option: {config_id}"}

        return {"configOptions": self._config_options(s)}

    # ── session/list ──────────────────────────────────────────────
    def handle_session_list(self, params: dict) -> dict:
        sessions = self.sessions.list_sessions()
        return {
            "sessions": [
                {"sessionId": s.id, "cwd": s.work_dir, "mode": s.mode,
                 "createdAt": s.created_at}
                for s in sessions
            ]
        }


# ── Main ─────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info(f"DeepSeek TUI → cc-connect  v3.9.1")
    log.info(f"  DEEPSEEK_BIN={DEEPSEEK_BIN}")
    log.info(f"  DEEPSEEK_WORKDIR={DEEPSEEK_WORKDIR}")
    log.info("=" * 60)

    transport = JSONRPCTransport()
    handlers = ACPHandlers(transport)

    transport.register("initialize", handlers.handle_initialize)
    transport.register("authenticate", handlers.handle_authenticate)
    transport.register("session/new", handlers.handle_session_new)
    transport.register("session/load", handlers.handle_session_load)
    transport.register("session/prompt", handlers.handle_session_prompt)
    transport.register("session/set_mode", handlers.handle_session_set_mode)
    transport.register("session/set_config_option", handlers.handle_session_set_config_option)
    transport.register("session/list", handlers.handle_session_list)

    def on_signal(signum, frame):
        log.info(f"Signal {signum}, shutting down.")
        transport.stop()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        transport.run()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Waiting for pending executions...")
        transport.wait_pending(handlers)
        log.info("Adapter shut down.")


if __name__ == "__main__":
    main()
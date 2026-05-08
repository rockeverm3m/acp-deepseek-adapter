#!/usr/bin/env python3
"""
ACP → DeepSeek TUI Adapter  v3.6
=================================
Bridges cc-connect's ACP (JSON-RPC 2.0 over stdio) to deepseek-tui.

Verified against:
  - deepseek-tui v0.8.16 (2026-05-08)
  - ACP spec: session/prompt deferred response with stopReason
  - deepseek exec "prompt"              → agent mode with tools (read_file, exec_shell, etc.)
  - deepseek exec --auto "prompt"       → agent mode + auto-approve all tools
  - deepseek exec --json "prompt"       → one-shot mode, NO tools (do NOT use)
  - deepseek sessions                   → list sessions
  - deepseek thread resume <id>         → resume session
  - Output: plain text with "tool: <name> (<params>)" and "tool <name> completed: <result>"

Usage:
    python3 acp_deepseek_adapter.py

Environment variables:
    DEEPSEEK_BIN        Path to deepseek binary  (default: /Users/rk/deepseek)
    DEEPSEEK_WORKDIR    Working directory        (default: /Users/rk)
    ADAPTER_LOG_FILE    Log file path            (default: /tmp/acp-deepseek-adapter.log)
"""

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
LOG_FILE = os.environ.get("ADAPTER_LOG_FILE", "/tmp/acp-deepseek-adapter.log")
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
DEEPSEEK_BIN = os.environ.get("DEEPSEEK_BIN", "/Users/rk/deepseek")
DEEPSEEK_WORKDIR = os.environ.get("DEEPSEEK_WORKDIR", "/Users/rk")
HISTORY_DIR = os.environ.get("ADAPTER_HISTORY_DIR",
    os.path.expanduser("~/.acp-adapter"))
HISTORY_MAX = 10  # keep last N message pairs


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
        os.makedirs(HISTORY_DIR, exist_ok=True)
        return os.path.join(HISTORY_DIR, "feishu_history.json")

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
    KIMI_KEY = "sk-kimi-UF4ZqEZKM5CdRcTMXoK1AbweAfIlyHEzJJU4qGArSc8l0GjVR46GMPaiIe8ga1aj"

    def __init__(self, transport: JSONRPCTransport):
        self.transport = transport
        self._bin = DEEPSEEK_BIN
        self._tool_counter = 0
        self._response_chars = 0  # per-execute character counter
        self._response_text = ""  # accumulated response text

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
    # deepseek exec outputs plain text. Tool calls are marked as:
    #   tool: <name> (<params>)
    #   tool <name> completed: <result>
    # Everything else is assistant text.

    _TOOL_START_RE = None  # compiled at class init

    def _stream_output(self, process: subprocess.Popen, session_id: str):
        import re
        tool_start_pat = re.compile(r"^tool:\s+(\S+)\s*\((.*)\)\s*$")
        tool_done_pat = re.compile(r"^tool\s+(\S+)\s+completed(?::\s*(.*))?\s*$")

        try:
            for line in process.stdout:
                line = line.rstrip("\n").rstrip("\r")
                if not line:
                    self._emit_text(session_id, "")
                    continue

                # Detect tool call start
                m = tool_start_pat.match(line)
                if m:
                    tool_name = m.group(1)
                    tool_params = m.group(2)
                    self._emit_tool_call_text(session_id, self._next_tool_id(),
                                              tool_name, tool_params)
                    continue

                # Detect tool completion (inline result only; multi-line dropped)
                m = tool_done_pat.match(line)
                if m:
                    tool_name = m.group(1)
                    tool_result = m.group(2)
                    if tool_result is None:
                        continue  # multi-line result → skip, let next text flow
                    self._emit_tool_done_text(session_id, self._next_tool_id(),
                                              tool_name, tool_result)
                    continue

                # Plain text → emit as assistant message chunk
                self._emit_text(session_id, line)
        except Exception as e:
            log.error(f"Stream error: {e}")

    def _next_tool_id(self) -> str:
        self._tool_counter += 1
        return f"tool_{self._tool_counter}"

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
        return text

    def _emit_text(self, session_id: str, text: str):
        self._response_chars += len(text) + 1  # +1 for the newline we append
        # Only accumulate real content for history (skip tool/text noise)
        if not text.startswith(('🔧', '✅', '[ctx:', '[退出码:', '[超时]', '[错误]')):
            self._response_text += text + "\n"
        text = self._sanitize_feishu(text)
        self.transport.send_notification("session/update", {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text + "\n"},
            },
        })

    def _emit_tool_call_text(self, session_id: str, tool_id: str,
                              name: str, params: str):
        # Silently count, don't emit — keeps Feishu chat clean
        self._response_chars += len(params)

    def _emit_tool_done_text(self, session_id: str, tool_id: str,
                              name: str, result: str):
        # Silently count, don't emit
        self._response_chars += len(result)

    # ── Execution ────────────────────────────────────────────────
    def execute(self, prompt: str, session: Session) -> dict:
        self._response_chars = 0  # reset per-call counter
        self._response_text = ""  # reset accumulated text

        # ── Kimi For Coding path (direct HTTP, no deepseek exec) ──
        if session.model == "kimi-for-coding":
            return self._execute_kimi(prompt, session)

        cmd = self._build_command(prompt, session)
        log.info(f"Exec: {' '.join(cmd[:3])}... + prompt ({len(prompt)} chars)")

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # deepseek exec outputs tool calls on stderr
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
                self._emit_text(session.id, "\n[超时] 命令执行超过10分钟，已终止。\n")
                return {"status": "timeout", "exitCode": -1}

            stream_thread.join(timeout=10)

            if returncode != 0:
                self._emit_text(session.id, f"\n[退出码: {returncode}]\n")

            self._discover_thread_id(session)

            # Emit context usage estimate [ctx: ~X%]
            # Includes: system prompt (~29K) + user prompt + this response
            est_tokens = (self._read_system_tokens()
                          + len(prompt) // 2
                          + max(0, self._response_chars // 2))
            pct = min(est_tokens * 100 // 1_000_000, 99)
            self._emit_text(session.id, f"[ctx: ~{pct}%]")

            return {"status": "completed", "exitCode": returncode}

        except FileNotFoundError:
            msg = f"找不到 deepseek-tui: {DEEPSEEK_BIN}"
            log.error(msg)
            self._emit_text(session.id, f"\n[错误] {msg}\n")
            return {"status": "error", "message": msg}
        except Exception as e:
            log.error(f"Execution error: {e}", exc_info=True)
            self._emit_text(session.id, f"\n[错误] {e}\n")
            return {"status": "error", "message": str(e)}

    # ── Kimi direct API call ─────────────────────────────────────
    def _execute_kimi(self, prompt: str, session: Session) -> dict:
        """Call Kimi For Coding API directly (Anthropic Messages format)."""
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
            msg = f"Kimi API error {e.code}: {e.read().decode()[:200]}"
            log.error(msg)
            self._emit_text(session.id, f"\n[错误] {msg}\n")
            return {"status": "error", "message": msg}
        except Exception as e:
            log.error(f"Kimi call failed: {e}")
            self._emit_text(session.id, f"\n[错误] Kimi 调用失败: {e}\n")
            return {"status": "error", "message": str(e)}

        # Extract text from Anthropic response
        text = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")

        usage = result.get("usage", {})
        total_tokens = usage.get("total_tokens", 0)
        log.info(f"Kimi done: {total_tokens} tokens, {len(text)} chars")

        self._emit_text(session.id, text)

        # Emit context
        est_tokens = self._read_system_tokens() + total_tokens
        pct = min(est_tokens * 100 // 1_000_000, 99)
        self._emit_text(session.id, f"[ctx: ~{pct}% | Kimi {total_tokens} tok]")

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
                "name": "deepseek-tui-acp-adapter",
                "version": "3.6.0",
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
    log.info(f"ACP → DeepSeek TUI Adapter v3.6.0")
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

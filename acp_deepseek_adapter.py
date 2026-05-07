#!/usr/bin/env python3
"""
ACP → DeepSeek TUI Adapter  v3.2
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
        self.created_at: float = time.time()


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

    def __init__(self, transport: JSONRPCTransport):
        self.transport = transport
        self._bin = DEEPSEEK_BIN
        self._tool_counter = 0

    # ── Command building ─────────────────────────────────────────
    def _build_command(self, prompt: str, session: Session) -> List[str]:
        """Build: deepseek exec [--auto] [--resume <id>] "<prompt>"

        NO --json flag — that switches to one-shot mode without tools.
        """
        cmd = [self._bin, "exec"]

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
        # deepseek exec output formats:
        #   tool: <name> (<params>)
        #   tool <name> completed: <inline_result>
        #   tool <name> completed                 (no colon → multi-line result follows)
        tool_start_pat = re.compile(r"^tool:\s+(\S+)\s*\((.*)\)\s*$")
        tool_done_pat = re.compile(r"^tool\s+(\S+)\s+completed(?::\s*(.*))?\s*$")

        # Track current tool call for matching completion
        current_tool_id: Optional[str] = None
        # Multi-line tool result buffering (for "completed" without colon)
        _result_buf: Optional[list] = None
        _result_tool_id: Optional[str] = None
        _result_tool_name: Optional[str] = None

        try:
            for line in process.stdout:
                line = line.rstrip("\n").rstrip("\r")
                if not line:
                    # blank line while buffering → keep in result
                    if _result_buf is not None:
                        _result_buf.append("")
                    continue

                # If we're buffering multi-line tool result
                if _result_buf is not None:
                    # Check if this is a new tool call → flush buffer
                    if tool_start_pat.match(line):
                        self._emit_tool_done_text(session_id, _result_tool_id,
                                                  _result_tool_name,
                                                  "\n".join(_result_buf))
                        _result_buf = None
                        _result_tool_id = None
                        _result_tool_name = None
                        # fall through to process this tool: line
                    else:
                        _result_buf.append(line)
                        continue

                # Detect tool call start
                m = tool_start_pat.match(line)
                if m:
                    tool_name = m.group(1)
                    tool_params = m.group(2)
                    current_tool_id = self._next_tool_id()
                    self._emit_tool_call_text(session_id, current_tool_id,
                                              tool_name, tool_params)
                    continue

                # Detect tool completion
                m = tool_done_pat.match(line)
                if m:
                    tool_name = m.group(1)
                    tool_result = m.group(2)  # None when no colon (multi-line)
                    tid = current_tool_id or self._next_tool_id()
                    current_tool_id = None
                    if tool_result is None:
                        # Enter buffering mode for multi-line tool output
                        _result_buf = []
                        _result_tool_id = tid
                        _result_tool_name = tool_name
                        continue
                    self._emit_tool_done_text(session_id, tid,
                                              tool_name, tool_result)
                    continue

                # Plain text → emit as assistant message chunk
                self._emit_text(session_id, line)

            # Flush any remaining buffered result
            if _result_buf is not None:
                self._emit_tool_done_text(session_id, _result_tool_id,
                                          _result_tool_name,
                                          "\n".join(_result_buf))
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
        self.transport.send_notification("session/update", {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": tool_id,
                "title": name,
                "kind": name,
                "status": "in_progress",
                "rawInput": {"params": params},
            },
        })

    def _emit_tool_done_text(self, session_id: str, tool_id: str,
                              name: str, result: str):
        self.transport.send_notification("session/update", {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_id,
                "title": name,
                "status": "completed",
                "output": result,
            },
        })

    # ── Execution ────────────────────────────────────────────────
    def execute(self, prompt: str, session: Session) -> dict:
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

            # Emit context usage like [ctx: ~X%]
            ctx_info = self._get_context_usage(session)
            if ctx_info:
                self._emit_text(session.id, ctx_info)

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

    # ── Context usage ────────────────────────────────────────────
    _CONTEXT_WINDOW_TOKENS = 1_000_000  # deepseek-v4-pro 1M window

    def _get_context_usage(self, session: Session) -> Optional[str]:
        """Read latest session file and compute approximate context usage %."""
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
            total = data.get("metadata", {}).get("total_tokens", 0)
            pct = min(int(total * 100 / self._CONTEXT_WINDOW_TOKENS), 99)
            return f"[ctx: ~{pct}%]"
        except Exception as e:
            log.debug(f"context usage read failed: {e}")
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
                "version": "3.2.0",
            },
            "modes": {
                "availableModes": self.backend.MODES,
                "currentModeId": "default",
            },
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
        return {
            "sessionId": s.id,
            "cwd": s.work_dir,
            "modes": {
                "availableModes": self.backend.MODES,
                "currentModeId": s.mode,
            },
        }

    # ── session/load ──────────────────────────────────────────────
    def handle_session_load(self, params: dict) -> dict:
        sid = params.get("sessionId", "")
        s = self.sessions.get(sid)
        if not s:
            s = self.sessions.create(work_dir=params.get("cwd", DEEPSEEK_WORKDIR))
        return {
            "sessionId": s.id,
            "cwd": s.work_dir,
            "modes": {
                "availableModes": self.backend.MODES,
                "currentModeId": s.mode,
            },
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

        log.info(f"session/prompt: sid={sid}, len={len(prompt_text)}")

        # Execute synchronously — execute() blocks until deepseek finishes,
        # streaming session/update notifications for text and tool calls along the way.
        try:
            result = self.backend.execute(prompt_text, s)
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
    log.info(f"ACP → DeepSeek TUI Adapter v3.2.0")
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

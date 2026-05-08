#!/usr/bin/env python3
"""Kimi Anthropic ↔ OpenAI proxy for DeepSeek TUI.
Listens on localhost:8899, translates OpenAI /v1/chat/completions
requests into Anthropic /v1/messages for Kimi For Coding.
"""

import json, os, sys, requests
from http.server import HTTPServer, BaseHTTPRequestHandler

KIMI_URL = "https://api.kimi.com/coding/v1/messages"
KIMI_KEY = os.environ.get("KIMI_API_KEY", "")
KIMI_MODEL = os.environ.get("KIMI_MODEL", "kimi-for-coding")
PORT = int(os.environ.get("KIMI_PROXY_PORT", "8899"))


class ProxyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))

        # Extract OpenAI-format messages
        msgs = body.get("messages", [])
        max_tokens = body.get("max_tokens", 4096)
        temperature = body.get("temperature", 0.7)

        # Build Anthropic-format request
        system_msg = ""
        anthropic_msgs = []
        for m in msgs:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                system_msg = content
            else:
                anthropic_msgs.append({"role": role, "content": content})

        payload = {
            "model": KIMI_MODEL,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": anthropic_msgs,
        }
        if system_msg:
            payload["system"] = system_msg

        headers = {
            "x-api-key": KIMI_KEY,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }

        try:
            resp = requests.post(KIMI_URL, json=payload, headers=headers, timeout=300)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            self._json_response(500, {"error": str(e)})
            return

        # Translate Anthropic response → OpenAI format
        content_text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                content_text += block.get("text", "")

        openai_resp = {
            "id": data.get("id", ""),
            "object": "chat.completion",
            "model": data.get("model", KIMI_MODEL),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content_text},
                "finish_reason": data.get("stop_reason", "stop"),
            }],
            "usage": data.get("usage", {}),
        }
        self._json_response(200, openai_resp)

    def _json_response(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        print(f"[kimi-proxy] {args[0]}", file=sys.stderr)


if __name__ == "__main__":
    if not KIMI_KEY:
        print("ERROR: set KIMI_API_KEY environment variable", file=sys.stderr)
        sys.exit(1)
    print(f"[kimi-proxy] Starting on :{PORT} → {KIMI_URL}", file=sys.stderr)
    HTTPServer(("127.0.0.1", PORT), ProxyHandler).serve_forever()

"""A minimal MCP stdio client.

Enough of the protocol to hold a real tool-calling conversation: initialize,
tools/list, tools/call. It talks to whatever command it is given — and in this
experiment that command is `bollard run -- python env_server.py`, so every call
is recorded on the way past without the client knowing anything about it.
"""

from __future__ import annotations

import json
import subprocess
import threading
from typing import Any, Dict, List, Optional


class MCPClient:
    def __init__(self, command: List[str], timeout: float = 60.0) -> None:
        self.command = command
        self.timeout = timeout
        self._next_id = 0
        self._lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None

    def __enter__(self) -> "MCPClient":
        self.proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "bollard-experiment", "version": "1.0"},
        })
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if not self.proc:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.proc or not self.proc.stdin or not self.proc.stdout:
            raise RuntimeError("client is not running")
        with self._lock:
            self._next_id += 1
            message = {"jsonrpc": "2.0", "id": self._next_id, "method": method,
                       "params": params or {}}
            self.proc.stdin.write((json.dumps(message) + "\n").encode())
            self.proc.stdin.flush()

            while True:
                line = self.proc.stdout.readline()
                if not line:
                    raise RuntimeError(f"server closed the connection during {method}")
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("{"):
                    continue
                try:
                    reply = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if reply.get("id") == self._next_id:
                    return reply

    def list_tools(self) -> List[Dict[str, Any]]:
        return (self.request("tools/list").get("result") or {}).get("tools", [])

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        reply = self.request("tools/call", {"name": name, "arguments": arguments})
        if "error" in reply:
            return f"error: {reply['error'].get('message', 'unknown')}"
        content = (reply.get("result") or {}).get("content") or []
        parts = [c.get("text", "") for c in content if isinstance(c, dict)]
        return "\n".join(parts)


def to_openai_tools(mcp_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Translate MCP tool definitions into the OpenAI function-calling shape."""
    out = []
    for tool in mcp_tools:
        out.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("inputSchema")
                              or {"type": "object", "properties": {}},
            },
        })
    return out

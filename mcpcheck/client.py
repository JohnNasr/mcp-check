"""A minimal stdio MCP client: spawn a server, speak newline-delimited JSON-RPC, time each request."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time

PROTOCOL_VERSION = "2025-06-18"


class MCPError(Exception):
    """Transport trouble: could not start, timed out, or the server exited."""


class RPCError(MCPError):
    """The server answered with a JSON-RPC error (it is alive and replying)."""


class StdioClient:
    def __init__(self, command: list[str], env: dict | None = None, cwd: str | None = None, timeout: float = 30):
        exe = shutil.which(command[0]) or command[0]  # resolves npx -> npx.cmd on Windows
        self.timeout = timeout
        self.stdout_noise: list[str] = []  # non-JSON lines on stdout break real clients
        self.stderr: list[str] = []
        self._next_id = 0
        self._lines: queue.Queue = queue.Queue()
        try:
            self.proc = subprocess.Popen([exe, *command[1:]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, cwd=cwd, env={**os.environ, **(env or {})},
                                         text=True, encoding="utf-8", errors="replace", bufsize=1)
        except OSError as e:
            raise MCPError(f"Could not start {command[0]!r}: {e}") from e
        threading.Thread(target=self._pump, args=(self.proc.stdout, self._lines), daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    @staticmethod
    def _pump(stream, q):
        for line in stream:
            q.put(line)
        q.put(None)

    def _pump_stderr(self):
        for line in self.proc.stderr:
            self.stderr.append(line.rstrip())

    def _send(self, msg: dict) -> None:
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except OSError as e:
            raise MCPError(f"Server closed its input: {e}. Last stderr: {self.last_stderr()}") from e

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def request(self, method: str, params: dict | None = None) -> tuple[dict, float]:
        """Send a request; return (result, seconds). Raises MCPError on error, timeout or exit."""
        self._next_id += 1
        rid = self._next_id
        start = time.perf_counter()
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        deadline = start + self.timeout
        while True:
            try:
                line = self._lines.get(timeout=max(0.0, deadline - time.perf_counter()))
            except queue.Empty:
                raise MCPError(f"{method}: no reply within {self.timeout:.0f}s") from None
            if line is None:
                raise MCPError(f"{method}: server exited (code {self.proc.poll()}). Last stderr: {self.last_stderr()}")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self.stdout_noise.append(line[:200])
                continue
            if msg.get("id") != rid or "method" in msg:
                if "method" in msg and "id" in msg:  # server->client request (e.g. roots/list): decline politely
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported"}})
                continue
            if "error" in msg:
                err = msg["error"]
                raise RPCError(f"{method}: error {err.get('code')}: {err.get('message')}")
            return msg.get("result") or {}, time.perf_counter() - start

    def last_stderr(self, n: int = 3) -> str:
        return " | ".join(self.stderr[-n:]) or "(none)"

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()

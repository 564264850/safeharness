"""Bounded tools-only stdio client for a host-selected MCP 2025-11-25 server.

Each call has one disposable session. No server requests, sampling, prompts,
resources, remote HTTP, dynamic grants or MCP writes are implemented.
"""

import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass

from .execution import ToolResult
from .invocations import digest

PROTOCOL_VERSION = "2025-11-25"


class MCPError(RuntimeError):
    pass


@dataclass(frozen=True)
class StdioServer:
    command: tuple[str, ...]
    cwd: str
    version: str
    timeout_seconds: float = 5
    max_output_bytes: int = 16384

    def __post_init__(self):
        if (
            not self.command
            or not os.path.isabs(self.command[0])
            or not os.path.isabs(self.cwd)
        ):
            raise ValueError(
                "Host must configure absolute command and working directory"
            )
        if (
            not self.version
            or not 0 < self.timeout_seconds <= 30
            or not 128 <= self.max_output_bytes <= 131072
        ):
            raise ValueError("Invalid MCP version or resource bounds")

    @property
    def fingerprint(self):
        return digest(
            {
                "command": self.command,
                "cwd": self.cwd,
                "version": self.version,
                "protocol": PROTOCOL_VERSION,
                "timeout": self.timeout_seconds,
                "output_limit": self.max_output_bytes,
            }
        )


class ReadOnlyMCP:
    def __init__(self, server: StdioServer):
        self.server = server

    def call(self, public_name, remote_name, schema, arguments):
        try:
            value = self._call(remote_name, schema, arguments)
            return ToolResult(public_name, "ok", "mcp_tool_read", value)
        except MCPError as error:
            return ToolResult(public_name, "failed", str(error))

    def _call(self, name, schema, arguments):
        config = self.server
        # Deliberately omit API keys and inherited application credentials.
        environment = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "LANG"}
        }
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        try:
            process = subprocess.Popen(
                config.command,
                cwd=config.cwd,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
        except OSError:
            raise MCPError("mcp_start_failed") from None
        mailbox = queue.Queue(maxsize=4)
        deadline = time.monotonic() + config.timeout_seconds

        def read():
            try:
                total = 0
                while True:
                    line = process.stdout.readline(config.max_output_bytes + 1)
                    total += len(line)
                    if (
                        len(line) > config.max_output_bytes
                        or total > config.max_output_bytes
                    ):
                        mailbox.put(MCPError("mcp_output_limit"), timeout=0.1)
                        return
                    if not line:
                        mailbox.put(MCPError("mcp_disconnected"), timeout=0.1)
                        return
                    mailbox.put(line, timeout=0.1)
            except (OSError, ValueError, queue.Full):
                return

        reader = threading.Thread(target=read, daemon=True)
        reader.start()

        def send(value):
            if time.monotonic() >= deadline:
                raise MCPError("mcp_timeout")
            data = (
                json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
                + b"\n"
            )
            if len(data) > 8192:
                raise MCPError("mcp_input_limit")
            try:
                process.stdin.write(data)
                process.stdin.flush()
            except (OSError, ValueError):
                raise MCPError("mcp_disconnected") from None

        def request(identifier, method, params):
            send(
                {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
            )
            try:
                line = mailbox.get(timeout=max(0.001, deadline - time.monotonic()))
            except queue.Empty:
                raise MCPError("mcp_timeout") from None
            if isinstance(line, MCPError):
                raise line
            try:
                reply = json.loads(line)
                if (
                    not isinstance(reply, dict)
                    or reply.get("jsonrpc") != "2.0"
                    or type(reply.get("id")) is not int
                    or reply["id"] != identifier
                ):
                    raise ValueError
                if "error" in reply:
                    raise MCPError("mcp_protocol_error")
                result = reply["result"]
                if not isinstance(result, dict):
                    raise TypeError
                return result
            except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
                raise MCPError("mcp_protocol_error") from None

        try:
            hello = request(
                1,
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "safeharness", "version": "0.1.0"},
                },
            )
            if (
                hello.get("protocolVersion") != PROTOCOL_VERSION
                or not isinstance(hello.get("capabilities"), dict)
                or "tools" not in hello["capabilities"]
            ):
                raise MCPError("mcp_capability_mismatch")
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            listing = request(2, "tools/list", {})
            tools = listing.get("tools")
            if (
                not isinstance(tools, list)
                or len(tools) > 32
                or listing.get("nextCursor")
            ):
                raise MCPError("mcp_discovery_limit")
            selected = [
                tool
                for tool in tools
                if isinstance(tool, dict) and tool.get("name") == name
            ]
            if len(selected) != 1 or selected[0].get("inputSchema") != schema:
                raise MCPError("mcp_schema_mismatch")
            # Server annotations, description and instructions do not grant access.
            result = request(3, "tools/call", {"name": name, "arguments": arguments})
            if result.get("isError", False) is not False:
                raise MCPError("mcp_tool_error")
            content = result.get("content")
            if not isinstance(content, list) or not content:
                raise MCPError("mcp_protocol_error")
            texts = []
            for block in content:
                if (
                    not isinstance(block, dict)
                    or block.get("type") != "text"
                    or not isinstance(block.get("text"), str)
                ):
                    raise MCPError("mcp_content_unsupported")
                texts.append(block["text"])
            value = "\n".join(texts)
            if len(value.encode("utf-8")) > config.max_output_bytes:
                raise MCPError("mcp_output_limit")
            return value
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdin.close()
            reader.join(timeout=1)
            process.stdout.close()

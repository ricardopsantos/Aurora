#!/usr/bin/env python3
"""Minimal MCP (stdio, JSON-RPC 2.0) server for R170h's env-allowlist test —
a dedicated fixture (not fake_mcp_server.py) so its extra tool doesn't shift
tool-count assertions elsewhere. One tool: `getenv(name) -> value or
"<unset>"`, reporting THIS process's own environment."""
import json
import os
import sys


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg_id,
                 "result": {"protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "serverInfo": {"name": "envcheck", "version": "0"}}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [
                {"name": "getenv",
                 "description": "reports whether an env var is set in this process",
                 "inputSchema": {"type": "object",
                                 "properties": {"name": {"type": "string"}},
                                 "required": ["name"]}}]}})
        elif method == "tools/call":
            params = msg.get("params", {})
            if params.get("name") == "getenv":
                var = params.get("arguments", {}).get("name", "")
                value = os.environ.get(var, "<unset>")
                send({"jsonrpc": "2.0", "id": msg_id,
                     "result": {"content": [{"type": "text", "text": value}],
                               "isError": False}})
            else:
                send({"jsonrpc": "2.0", "id": msg_id,
                     "error": {"code": -32601, "message": "unknown tool"}})
        else:
            send({"jsonrpc": "2.0", "id": msg_id,
                 "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()

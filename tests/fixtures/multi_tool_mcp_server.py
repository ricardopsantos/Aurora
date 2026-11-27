#!/usr/bin/env python3
"""Minimal MCP (stdio) server with three tools — `echo`, `ping`, `shout` —
so the `tools:` allowlist (R449) has something to filter. Same protocol
subset as fake_mcp_server.py."""
import json
import sys

_TOOLS = [{"name": n, "description": f"{n} tool",
           "inputSchema": {"type": "object",
                           "properties": {"text": {"type": "string"}}}}
          for n in ("echo", "ping", "shout")]


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method, msg_id = msg.get("method"), msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg_id,
                  "result": {"protocolVersion": "2024-11-05",
                             "capabilities": {},
                             "serverInfo": {"name": "multi", "version": "0"}}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg_id,
                  "result": {"tools": _TOOLS}})
        elif method == "tools/call":
            p = msg.get("params", {})
            text = p.get("arguments", {}).get("text", "")
            send({"jsonrpc": "2.0", "id": msg_id,
                  "result": {"content": [{"type": "text",
                                          "text": f"{p.get('name')}: {text}"}],
                             "isError": False}})
        else:
            send({"jsonrpc": "2.0", "id": msg_id,
                  "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Minimal MCP (stdio, JSON-RPC 2.0) server for tests — no network, no npm.
Implements just enough of the protocol (initialize, tools/list, tools/call)
to exercise aurora.mcp's client against a real subprocess instead of a
mock, with one tool: `echo(text) -> "echo: <text>"`."""
import json
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
                            "serverInfo": {"name": "fake", "version": "0"}}})
        elif method == "notifications/initialized":
            continue   # notifications get no response
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [
                {"name": "echo", "description": "echoes the given text back",
                 "inputSchema": {"type": "object",
                                 "properties": {"text": {"type": "string"}},
                                 "required": ["text"]}}]}})
        elif method == "tools/call":
            params = msg.get("params", {})
            if params.get("name") == "echo":
                text = params.get("arguments", {}).get("text", "")
                send({"jsonrpc": "2.0", "id": msg_id,
                     "result": {"content": [{"type": "text",
                                             "text": f"echo: {text}"}],
                               "isError": False}})
            else:
                send({"jsonrpc": "2.0", "id": msg_id,
                     "error": {"code": -32601, "message": "unknown tool"}})
        else:
            send({"jsonrpc": "2.0", "id": msg_id,
                 "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()

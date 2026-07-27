#!/usr/bin/env python3
"""MCP test fixture that answers initialize/tools-list normally (so
MCPServer.__init__ succeeds) but then hangs forever on tools/call — no
reply, ever. Used to prove aurora.mcp's read timeout fires against a
SILENT server, not just a slow one (readline() blocks; the fix must
enforce the deadline on the wait itself, via select)."""
import json
import sys
import time


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
                            "serverInfo": {"name": "hanging", "version": "0"}}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [
                {"name": "hang", "description": "never replies",
                 "inputSchema": {"type": "object", "properties": {}}}]}})
        elif method == "tools/call":
            time.sleep(600)   # simulate a wedged server — no reply, ever
        else:
            send({"jsonrpc": "2.0", "id": msg_id,
                 "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()

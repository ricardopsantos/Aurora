#!/usr/bin/env python3
"""MCP (stdio) server that emits a NOTIFICATION line immediately before each
reply, both flushed in one burst — the R126 regression fixture.

This is ordinary, spec-legal MCP behaviour (`notifications/*` are part of the
protocol, and real servers log progress this way). It is also exactly the
shape that broke the old client: both lines land in one OS read, so the
reply ends up buffered inside Python where `select()` on the file descriptor
can never see it. The old code read the notification, selected again, saw an
idle fd, and timed out on a reply it was already holding.

The `time.sleep` after each burst keeps the fd genuinely quiet afterwards, so
a client that depends on further fd activity to drain its own buffer hangs
rather than accidentally passing."""
import json
import sys
import time


def burst(msgs):
    """Write several lines and flush ONCE — they reach the client together."""
    for m in msgs:
        sys.stdout.write(json.dumps(m) + "\n")
    sys.stdout.flush()
    time.sleep(0.05)   # nothing more arrives on the fd for a while


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        msg_id = msg.get("id")
        note = {"jsonrpc": "2.0", "method": "notifications/message",
                "params": {"level": "info", "data": f"handling {method}"}}
        if method == "initialize":
            burst([note, {"jsonrpc": "2.0", "id": msg_id,
                          "result": {"protocolVersion": "2024-11-05",
                                     "capabilities": {},
                                     "serverInfo": {"name": "chatty",
                                                    "version": "0"}}}])
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            burst([note, {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [
                {"name": "echo", "description": "echoes the given text back",
                 "inputSchema": {"type": "object",
                                 "properties": {"text": {"type": "string"}},
                                 "required": ["text"]}}]}}])
        elif method == "tools/call":
            params = msg.get("params", {})
            text = params.get("arguments", {}).get("text", "")
            burst([note, {"jsonrpc": "2.0", "id": msg_id,
                          "result": {"content": [{"type": "text",
                                                  "text": f"echo: {text}"}],
                                     "isError": False}}])
        else:
            burst([{"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32601,
                              "message": f"unknown method {method}"}}])


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""MCP server whose tools/list mixes usable and unusable entries (R187a).

One good tool, plus entries Aurora cannot build a spec from: no "name" key,
a non-string name, a blank name, and a bare string instead of an object. The
good tool must survive; each bad entry must be dropped with a warning rather
than raising out of specs()/runners().

`--not-a-list` instead returns tools/list as an object, which is a
protocol-level breach rather than one bad entry.
"""
import json
import sys

NOT_A_LIST = "--not-a-list" in sys.argv


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
                            "serverInfo": {"name": "malformed", "version": "0"}}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            if NOT_A_LIST:
                send({"jsonrpc": "2.0", "id": msg_id,
                     "result": {"tools": {"echo": "not a list"}}})
                continue
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [
                {"name": "good", "description": "a usable tool",
                 "inputSchema": {"type": "object", "properties": {}}},
                {"description": "no name key at all"},
                {"name": 42, "description": "name isn't a string"},
                {"name": "   ", "description": "name is blank"},
                "i am a bare string, not an object",
            ]}})
        elif method == "tools/call":
            params = msg.get("params", {})
            if params.get("name") == "good":
                send({"jsonrpc": "2.0", "id": msg_id,
                     "result": {"content": [{"type": "text", "text": "ok"}]}})
            else:
                send({"jsonrpc": "2.0", "id": msg_id,
                     "error": {"code": -32601, "message": "no such tool"}})


if __name__ == "__main__":
    main()

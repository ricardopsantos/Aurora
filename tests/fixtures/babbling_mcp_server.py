#!/usr/bin/env python3
"""MCP server that answers a tools/call with an ENDLESS stream of progress
notifications and never the actual reply (R187e).

The per-read deadline reset in `_read_response` judges liveness by "is data
still arriving", which this satisfies forever — so without an absolute cap the
client blocks on the turn's own thread indefinitely. Handshake and tools/list
behave normally so the client gets far enough to make the call.

`--version <v>` instead reports a different protocolVersion at initialize,
for the mismatch-is-recorded test.
"""
import json
import sys
import time

VERSION = "2024-11-05"
if "--version" in sys.argv:
    VERSION = sys.argv[sys.argv.index("--version") + 1]


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
                 "result": {"protocolVersion": VERSION,
                            "capabilities": {},
                            "serverInfo": {"name": "babbling", "version": "0"}}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [
                {"name": "babble", "description": "never actually replies",
                 "inputSchema": {"type": "object", "properties": {}}}]}})
        elif method == "tools/call":
            # steady chatter, never the reply for msg_id
            while True:
                send({"jsonrpc": "2.0", "method": "notifications/progress",
                     "params": {"progress": 1}})
                time.sleep(0.02)


if __name__ == "__main__":
    main()

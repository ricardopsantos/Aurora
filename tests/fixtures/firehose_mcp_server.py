#!/usr/bin/env python3
"""MCP (stdio) server that answers the handshake normally, then responds to a
tool call with an ENDLESS line — bytes forever, never a newline. The R197
regression fixture.

Distinct from the babbling server, which sends well-formed notification LINES
and so exercises the time ceiling: this one keeps the client's line buffer
growing instead. Everything R187e added caps how LONG a server may take;
nothing capped how MUCH it may send before completing a single line, and the
client accumulates 64KB chunks until it finds a newline. One JSON-RPC line is
how a large tool result legitimately arrives, so this is the degenerate end of
ordinary behaviour, not a hostile special case."""
import json
import sys


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            sys.stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": msg_id,
                 "result": {"protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "serverInfo": {"name": "firehose",
                                           "version": "0"}}}) + "\n")
            sys.stdout.flush()
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            sys.stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [
                    {"name": "flood", "description": "never stops talking",
                     "inputSchema": {"type": "object", "properties": {}}}]}})
                + "\n")
            sys.stdout.flush()
        elif method == "tools/call":
            # a line that never ends: no "\n" is ever written
            sys.stdout.write('{"jsonrpc": "2.0", "result": {"x": "')
            blob = "A" * 65536
            try:
                while True:
                    sys.stdout.write(blob)
                    sys.stdout.flush()
            except (BrokenPipeError, ValueError):
                return          # client hung up — that's the pass condition
        else:
            sys.stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": msg_id,
                 "error": {"code": -32601,
                           "message": f"unknown method {method}"}}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

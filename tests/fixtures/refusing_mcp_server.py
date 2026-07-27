#!/usr/bin/env python3
"""An MCP server that REFUSES the handshake and then stays alive (R145a).

`initialize` gets a JSON-RPC error reply instead of a result, which is what
a real protocol-version mismatch looks like. The process then keeps running
— that is the whole point of the fixture: the client must kill it rather
than leave it orphaned, and a server that exited on its own would prove
nothing."""
import json
import sys
import time


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("method") == "initialize":
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": msg.get("id"),
                "error": {"code": -32602,
                          "message": "unsupported protocol version"}}) + "\n")
            sys.stdout.flush()
            time.sleep(300)     # outlive the test unless we are killed
            return


if __name__ == "__main__":
    main()
